# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# mypy: ignore-errors
"""Model base, mixin classes, and helper types for Superset.

Pure SQLAlchemy -- no legacy WSGI dependencies.
Includes ExploreMixin: the core SQL-generation engine for all chart queries.
"""

from __future__ import annotations

import builtins
import dataclasses
import json
import logging
import re
import uuid
from collections.abc import Callable, Hashable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, cast, NamedTuple, TYPE_CHECKING, Union

import dateutil.parser
import numpy as np
import pandas as pd
import sqlalchemy as sa
from jinja2.exceptions import TemplateError
from sqlalchemy import and_, or_, Text, types as sa_types
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import DeclarativeBase, relationship, validates
from sqlalchemy.sql.elements import ColumnElement, literal_column, TextClause
from sqlalchemy.sql.expression import Label, Select, TextAsFrom
from sqlalchemy.sql.selectable import Alias, TableClause

from superset.constants import EMPTY_STRING, NULL_STRING
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    AdvancedDataTypeResponseError,
    ColumnNotFoundException,
    QueryClauseValidationException,
    QueryObjectValidationError,
    SupersetSecurityException,
    SupersetSyntaxErrorException,
)
from superset.sql.parse import sanitize_clause, SQLScript, SQLStatement
from superset.utils.column import (
    get_column_name,
    get_column_names,
    get_metric_name,
    get_non_base_axis_columns,
    is_adhoc_column,
    is_adhoc_metric,
    remove_duplicates,
)

if TYPE_CHECKING:
    from superset.db_engine_specs.base import BaseEngineSpec, TimestampExpression
    from superset.models.connectors import SqlMetric, TableColumn
    from superset.models.core import Database

logger = logging.getLogger(__name__)


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base for all Superset models.

    ``AsyncAttrs`` adds the ``awaitable_attrs`` accessor
    (``await obj.awaitable_attrs.roles``) for point-wise relationship loads
    in async context — purely additive, no behaviour change for normal
    attribute access.

    Lazy-loading policy: relationships keep the default ``lazy="select"``
    because the models are SHARED between the async request path and the
    sync Celery/Jinja helpers — Celery tasks (e.g. ``tasks/sql_lab.py``'s
    ``query.database``) legitimately lazy-load via sync sessions.
    ``lazy="raise"`` is therefore NOT applicable here; async
    callers must eager-load (selectinload) or use ``awaitable_attrs``, and
    a missed load in async context fails loudly as MissingGreenlet, which
    the aiosqlite test suite reproduces deterministically.
    """

    __allow_unmapped__ = True


metadata = Base.metadata


# Column type helpers


def MediumText() -> sa_types.Text:  # noqa: N802
    return Text().with_variant(MEDIUMTEXT(), "mysql")


def LongText() -> sa_types.Text:  # noqa: N802
    return Text().with_variant(LONGTEXT(), "mysql")


def convert_uuids(obj: Any) -> Any:
    """
    Convert UUID objects to str so we can use yaml.safe_dump
    """
    if isinstance(obj, uuid.UUID):
        return str(obj)

    if isinstance(obj, list):
        return [convert_uuids(el) for el in obj]

    if isinstance(obj, dict):
        return {k: convert_uuids(v) for k, v in obj.items()}

    return obj


class BinaryUUID(sa_types.TypeDecorator[uuid.UUID]):
    """UUID stored as 16 bytes (bytea on PostgreSQL, BINARY(16) on MySQL).

    Drop-in replacement for ``sqlalchemy_utils.UUIDType(binary=True)``
    that works correctly with both asyncpg (binary protocol) and
    psycopg2 (text protocol).
    """

    impl = sa_types.LargeBinary(length=16)
    cache_ok = True

    def process_bind_param(
        self, value: uuid.UUID | str | bytes | None, dialect: Any
    ) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = uuid.UUID(value)
        if isinstance(value, uuid.UUID):
            return value.bytes
        return value  # already bytes

    def process_result_value(
        self, value: bytes | memoryview | None, dialect: Any
    ) -> uuid.UUID | None:
        """Decode bytea → UUID. Tolerate corrupt DB rows.

        A single bad uuid byte string (e.g. legacy data, a manual SQL
        repair stored as text-as-bytea, or column-width mismatch) used
        to abort the whole result row via ``ValueError: bytes is not a
        16-char string`` — which propagated to ``GET /api/v1/chart/`` /
        ``/dashboard/`` list endpoints as a 500. Log a warning and
        return ``None`` so the rest of the row still loads — the caller
        can decide whether a NULL uuid is fatal for *that* model.
        """
        if value is None:
            return None
        if isinstance(value, memoryview):
            value = bytes(value)
        try:
            if isinstance(value, bytes):
                if len(value) == 16:
                    return uuid.UUID(bytes=value)
                # ``bytea`` storing a UUID-string form (e.g. legacy/manual
                # repair). Try string parse before giving up.
                return uuid.UUID(value.decode("ascii", errors="replace"))
            if isinstance(value, uuid.UUID):
                return value
            return uuid.UUID(str(value))
        except (ValueError, TypeError):
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "UUIDType: malformed bytes in DB (len=%s) — returning None",
                len(value) if hasattr(value, "__len__") else "?",
            )
            return None


# Mixins


class UUIDMixin:
    """Adds a ``uuid`` column (unique, non-PK by default)."""

    uuid = sa.Column(
        BinaryUUID(),
        primary_key=False,
        unique=True,
        default=uuid.uuid4,
    )


class AuditMixinNullable:
    """Audit columns: created/changed timestamps and user FKs.

    Replaces the upstream AuditMixin with pure SQLAlchemy columns.
    All fields are nullable so legacy rows without audit data still load.

    Computed properties match the original upstream AuditMixin so that
    ``serialize_list_response`` can read them via ``getattr``.
    """

    created_on = sa.Column(sa.DateTime, default=datetime.now, nullable=True)
    changed_on = sa.Column(
        sa.DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=True,
    )

    @declared_attr
    def created_by_fk(cls):  # noqa: N805
        return sa.Column(
            sa.Integer,
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        )

    @declared_attr
    def changed_by_fk(cls):  # noqa: N805
        return sa.Column(
            sa.Integer,
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        )

    @declared_attr
    def created_by(cls):  # noqa: N805
        class_name: str = getattr(cls, "__name__", type(cls).__name__)
        return relationship(
            "User",
            primaryjoin=f"{class_name}.created_by_fk == User.id",
            foreign_keys=f"[{class_name}.created_by_fk]",
            viewonly=True,
        )

    @declared_attr
    def changed_by(cls):  # noqa: N805
        class_name: str = getattr(cls, "__name__", type(cls).__name__)
        return relationship(
            "User",
            primaryjoin=f"{class_name}.changed_by_fk == User.id",
            foreign_keys=f"[{class_name}.changed_by_fk]",
            viewonly=True,
        )

    # -- Computed properties (match original upstream @renders decorators) -

    @property
    def changed_on_utc(self) -> str | None:
        if self.changed_on is None:
            return None
        import pytz  # type: ignore[import-untyped]

        dt = self.changed_on
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        else:
            dt = dt.astimezone(pytz.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f%z")

    @property
    def changed_on_delta_humanized(self) -> str | None:
        if self.changed_on is None:
            return None
        import humanize

        changed_on: datetime = self.changed_on  # type: ignore[assignment]
        return humanize.naturaltime(datetime.now() - changed_on)

    @property
    def created_on_delta_humanized(self) -> str | None:
        if self.created_on is None:
            return None
        import humanize

        created_on: datetime = self.created_on  # type: ignore[assignment]
        return humanize.naturaltime(datetime.now() - created_on)

    @property
    def changed_by_name(self) -> str:
        if self.changed_by:
            return str(self.changed_by)
        return ""

    def creator(self) -> str:
        """Return the formatted name of the user who created this object."""
        if self.created_by:
            return str(self.created_by)
        return ""


class ImportExportMixin(UUIDMixin):
    """Marker mixin for models that support YAML import/export.

    The actual import/export logic lives in superset.importexport.
    This mixin provides the ``uuid`` column, the export field declarations
    and a ported ``export_to_dict`` helper.
    """

    export_parent: str | None = None
    export_children: list[str] = []  # noqa: RUF012
    export_fields: list[str] = []  # noqa: RUF012
    extra_import_fields: list[str] = []  # noqa: RUF012

    @classmethod
    def export_schema(
        cls, recursive: bool = True, include_parent_ref: bool = False
    ) -> dict[str, Any]:
        """Return a schema description for the model — used by
        :func:`superset.utils.dict_import_export.export_schema_to_dict`.
        """
        parent_excludes: set[str] = set()
        if not include_parent_ref:
            parent_ref = cls.__mapper__.relationships.get(cls.export_parent)
            if parent_ref:
                parent_excludes = {column.name for column in parent_ref.local_columns}

        def formatter(column: sa.Column) -> str:
            return (
                f"{str(column.type)} Default ({column.default.arg})"
                if column.default
                else str(column.type)
            )

        schema: dict[str, Any] = {
            column.name: formatter(column)
            for column in cls.__table__.columns
            if (column.name in cls.export_fields and column.name not in parent_excludes)
        }
        if recursive:
            for column in cls.export_children:
                child_class = cls.__mapper__.relationships[column].argument.class_
                schema[column] = [
                    child_class.export_schema(
                        recursive=recursive, include_parent_ref=include_parent_ref
                    )
                ]
        return schema

    @property
    def params_dict(self) -> dict[Any, Any]:
        """Parsed ``params`` JSON column as a dict.

        Dashboard/Slice override this with model-specific behaviour; models
        like ``SqlaTable`` that only have a plain ``params`` column inherit
        this default.
        """
        raw = getattr(self, "params", None)
        if not raw:
            return {}
        try:
            # Strip trailing commas before closing braces/brackets so legacy
            # rows written by older tools parse.
            val = re.sub(r",[ \t\r\n]+}", "}", raw)
            val = re.sub(r",[ \t\r\n]+\]", "]", val)
            return json.loads(val) or {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def override(self, obj: Any) -> None:
        """Override the plain (``export_fields``) columns from ``obj``."""
        for field in obj.__class__.export_fields:
            setattr(self, field, getattr(obj, field))

    def copy(self) -> Any:
        """Create a relationship-free copy of this model.

        Used by the legacy V0 dashboard export to detach objects before serialising.
        """
        new_obj = self.__class__()
        new_obj.override(self)
        return new_obj

    def alter_params(self, **kwargs: Any) -> None:
        """Merge ``kwargs`` into the JSON ``params`` column."""
        params = self.params_dict
        params.update(kwargs)
        self.params = json.dumps(params)  # type: ignore[attr-defined]

    def remove_params(self, param_to_remove: str) -> None:
        """Drop a single key from the JSON ``params`` column."""
        params = self.params_dict
        params.pop(param_to_remove, None)
        self.params = json.dumps(params)  # type: ignore[attr-defined]

    def export_to_dict(
        self,
        recursive: bool = True,
        include_parent_ref: bool = False,
        include_defaults: bool = False,
        export_uuids: bool = False,
    ) -> dict[Any, Any]:
        """Serialize the model to a plain dict using ``export_fields``.

        Used by the theme/dashboard/chart export commands to render YAML.
        """
        export_fields = set(self.export_fields)
        if export_uuids:
            export_fields.add("uuid")
            export_fields.discard("id")

        cls = self.__class__
        parent_excludes: set[str] = set()
        if recursive and not include_parent_ref:
            parent_ref = cls.__mapper__.relationships.get(cls.export_parent)
            if parent_ref is not None:
                parent_excludes = {c.name for c in parent_ref.local_columns}

        dict_rep = {
            c.name: getattr(self, c.name)
            for c in cls.__table__.columns
            if (
                c.name in export_fields
                and c.name not in parent_excludes
                and (
                    include_defaults
                    or (
                        getattr(self, c.name) is not None
                        and (not c.default or getattr(self, c.name) != c.default.arg)
                    )
                )
            )
        }

        # Order keys by declaration order in export_fields (DSU pattern).
        order = {field: i for i, field in enumerate(self.export_fields)}
        decorated = sorted(
            ((order.get(k, len(order)), k) for k in dict_rep), key=lambda t: t[0]
        )
        dict_rep = {k: dict_rep[k] for _, k in decorated}

        if recursive:
            for cld in self.export_children:
                children = getattr(self, cld, None) or []
                dict_rep[cld] = sorted(
                    [
                        child.export_to_dict(
                            recursive=recursive,
                            include_parent_ref=include_parent_ref,
                            include_defaults=include_defaults,
                        )
                        for child in children
                    ],
                    key=lambda k: sorted(str(k.items())),
                )

        # Convert UUID values (incl. those nested in JSON-typed columns) to
        # plain strings so yaml/json serialise cleanly.
        return convert_uuids(dict_rep)


class ExtraJSONMixin:
    """Provides an ``extra_json`` Text column with a parsed ``extra`` property.

    ``extra_json`` stores a JSON string; ``extra`` parses/serialises it as a dict.
    """

    extra_json = sa.Column("extra_json", MediumText(), default="{}")

    @property
    def extra(self) -> dict[str, Any]:
        try:
            return json.loads(self.extra_json or "{}") or {}
        except (TypeError, json.JSONDecodeError) as exc:
            logger.error(
                "Unable to load an extra json: %r. Leaving empty.", exc, exc_info=True
            )
            return {}

    @extra.setter
    def extra(self, extras: dict[str, Any]) -> None:
        self.extra_json = json.dumps(extras)

    def set_extra_json_key(self, key: str, value: Any) -> None:
        """Set a single key in the extra JSON dict."""
        extra = self.extra
        extra[key] = value
        self.extra_json = json.dumps(extra)

    @validates("extra_json")
    def ensure_extra_json_is_not_none(
        self,
        _: str,
        value: dict[str, Any] | None,
    ) -> Any:
        if value is None:
            return "{}"
        return value

    def get_extra_dict(self) -> dict[str, Any]:
        """Alias for the ``extra`` property — used by callers that expect a method."""
        return self.extra


class CertificationMixin:
    """Mixin to add extra certification fields"""

    extra = sa.Column(sa.Text, default="{}")

    def get_extra_dict(self) -> dict[str, Any]:
        try:
            return json.loads(self.extra)
        except (TypeError, json.JSONDecodeError):
            return {}

    @property
    def is_certified(self) -> bool:
        return bool(self.get_extra_dict().get("certification"))

    @property
    def certified_by(self) -> str | None:
        return self.get_extra_dict().get("certification", {}).get("certified_by")

    @property
    def certification_details(self) -> str | None:
        return self.get_extra_dict().get("certification", {}).get("details")

    @property
    def warning_markdown(self) -> str | None:
        return self.get_extra_dict().get("warning_markdown")


# Constants used by ExploreMixin

VIRTUAL_TABLE_ALIAS = "virtual_table"
SERIES_LIMIT_SUBQ_ALIAS = "series_limit"
DTTM_ALIAS = "__timestamp"

# Enums used by ExploreMixin


class AdhocMetricExpressionType(StrEnum):
    SIMPLE = "SIMPLE"
    SQL = "SQL"


class FilterOperator(StrEnum):
    """Operators used by filter controls."""

    EQUALS = "=="
    NOT_EQUALS = "!="
    GREATER_THAN = ">"
    LESS_THAN = "<"
    GREATER_THAN_OR_EQUALS = ">="
    LESS_THAN_OR_EQUALS = "<="
    LIKE = "LIKE"
    NOT_LIKE = "NOT LIKE"
    ILIKE = "ILIKE"
    IS_NULL = "IS NULL"
    IS_NOT_NULL = "IS NOT NULL"
    IN = "IN"
    NOT_IN = "NOT IN"
    IS_TRUE = "IS TRUE"
    IS_FALSE = "IS FALSE"
    TEMPORAL_RANGE = "TEMPORAL_RANGE"


# Type aliases
AdhocMetric = dict[str, Any]
AdhocColumn = dict[str, Any]
ColumnTyping = Union[AdhocColumn, str]
FilterValue = Union[bool, datetime, float, int, str]
FilterValues = Union[FilterValue, list[FilterValue], tuple[FilterValue]]
Metric = Union[AdhocMetric, str]
OrderBy = tuple[Union[Metric, ColumnTyping], bool]
QueryObjectDict = dict[str, Any]
QueryObjectFilterClause = dict[str, Any]


# AdvancedDataTypeResponse (lightweight TypedDict replacement)

AdvancedDataTypeResponse = dict[str, Any]


# Query result types


class QueryResult:
    """Object returned by the query interface."""

    def __init__(
        self,
        df: pd.DataFrame,
        query: str,
        duration: timedelta,
        applied_template_filters: list[str] | None = None,
        applied_filter_columns: list[ColumnTyping] | None = None,
        rejected_filter_columns: list[ColumnTyping] | None = None,
        status: str = "success",
        error_message: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        from_dttm: datetime | None = None,
        to_dttm: datetime | None = None,
    ) -> None:
        self.df = df
        self.query = query
        self.duration = duration
        self.applied_template_filters = applied_template_filters or []
        self.applied_filter_columns = applied_filter_columns or []
        self.rejected_filter_columns = rejected_filter_columns or []
        self.status = status
        self.error_message = error_message
        self.errors = errors or []
        self.from_dttm = from_dttm
        self.to_dttm = to_dttm
        self.sql_rowcount = len(self.df.index) if not self.df.empty else 0


class QueryStringExtended(NamedTuple):
    applied_template_filters: list[str] | None
    applied_filter_columns: list[ColumnTyping]
    rejected_filter_columns: list[ColumnTyping]
    labels_expected: list[str]
    prequeries: list[str]
    sql: str


class SqlaQuery(NamedTuple):
    applied_template_filters: list[str]
    applied_filter_columns: list[ColumnTyping]
    rejected_filter_columns: list[ColumnTyping]
    cte: str | None
    extra_cache_keys: list[Any]
    labels_expected: list[str]
    prequeries: list[str]
    sqla_query: Select


# validate_adhoc_subquery


def validate_adhoc_subquery(
    sql: str,
    database: "Database",
    catalog: str | None,
    default_schema: str,
    engine: str,
) -> str:
    """Check if adhoc SQL contains sub-queries or nested sub-queries with table.

    If sub-queries are allowed, the adhoc SQL is modified to insert any
    applicable RLS predicates to it.

    :param sql: adhoc sql expression
    :raise SupersetSecurityException: if sql contains sub-queries and
        ALLOW_ADHOC_SUBQUERY is disabled
    """
    from superset.utils.feature_flags import feature_flag_manager
    from superset.utils.rls import apply_rls

    parsed_statement = SQLStatement(sql, engine)
    if parsed_statement.has_subquery():
        if not feature_flag_manager.is_feature_enabled("ALLOW_ADHOC_SUBQUERY"):
            raise SupersetSecurityException(
                SupersetError(
                    error_type=SupersetErrorType.ADHOC_SUBQUERY_NOT_ALLOWED_ERROR,
                    message="Custom SQL fields cannot contain sub-queries.",
                    level=ErrorLevel.ERROR,
                )
            )

        # enforce RLS rules in any relevant tables
        apply_rls(database, catalog, default_schema, parsed_statement)

    return parsed_statement.format()


# ExploreMixin -- the core SQL generation engine for all chart queries


class ExploreMixin:
    """Allows any SQLAlchemy model (Query, Table, etc.) to power a chart
    inside /explore.
    """

    sqla_aggregations: dict[str, Any] = {  # noqa: RUF012
        "COUNT_DISTINCT": lambda column_name: sa.func.COUNT(sa.distinct(column_name)),
        "COUNT": sa.func.COUNT,
        "SUM": sa.func.SUM,
        "AVG": sa.func.AVG,
        "MIN": sa.func.MIN,
        "MAX": sa.func.MAX,
    }
    fetch_values_predicate: str | None = None

    normalize_columns: bool = False

    @property
    def type(self) -> str:
        raise NotImplementedError()

    @property
    def db_extra(self) -> dict[str, Any] | None:
        raise NotImplementedError()

    def query(self, query_obj: QueryObjectDict) -> QueryResult:
        raise NotImplementedError()

    @property
    def database_id(self) -> int:
        raise NotImplementedError()

    @property
    def owners_data(self) -> list[Any]:
        raise NotImplementedError()

    @property
    def metrics(self) -> list[Any]:
        return []

    @property
    def uid(self) -> str:
        raise NotImplementedError()

    @property
    def is_rls_supported(self) -> bool:
        return False

    @property
    def cache_timeout(self) -> int:
        raise NotImplementedError()

    @property
    def column_names(self) -> list[str]:
        raise NotImplementedError()

    @property
    def offset(self) -> int:
        raise NotImplementedError()

    @property
    def main_dttm_col(self) -> str | None:
        raise NotImplementedError()

    @property
    def always_filter_main_dttm(self) -> bool | None:
        return False

    @property
    def dttm_cols(self) -> list[str]:
        raise NotImplementedError()

    @property
    def db_engine_spec(self) -> builtins.type["BaseEngineSpec"]:
        raise NotImplementedError()

    @property
    def database(self) -> "Database":
        raise NotImplementedError()

    @property
    def catalog(self) -> str:
        raise NotImplementedError()

    @property
    def schema(self) -> str:
        raise NotImplementedError()

    @property
    def sql(self) -> str:
        raise NotImplementedError()

    @property
    def columns(self) -> list[Any]:
        raise NotImplementedError()

    def get_extra_cache_keys(self, query_obj: dict[str, Any]) -> list[Hashable]:
        raise NotImplementedError()

    def get_template_processor(self, **kwargs: Any) -> Any:
        raise NotImplementedError()

    def get_fetch_values_predicate(
        self,
        template_processor: Any | None = None,
    ) -> TextClause:
        return self.fetch_values_predicate  # type: ignore[return-value]

    def get_sqla_row_level_filters(
        self,
        template_processor: Any | None = None,  # noqa: ARG002
    ) -> list[TextClause]:
        """RLS hook for ``ExploreMixin``-only datasources (SQL Lab Query).

        RLS is *not* applicable for datasources of type ``query`` (a SQL Lab
        result), so this returns an empty list.

        The real RLS implementation lives on
        :class:`superset.models.connectors.BaseDatasource` — which
        precedes :class:`ExploreMixin` in the :class:`SqlaTable` MRO, so
        dataset-backed datasources get the genuine filters while a
        :class:`~superset.models.sql_lab.Query` (mixing in only
        ``ExploreMixin``) falls through to this stub.
        """
        # TODO: We should refactor this mixin and remove this method
        # as it exists in the BaseDatasource and is not applicable
        # for datasources of type query
        return []

    def _process_sql_expression(
        self,
        expression: str | None,
        database_id: int,
        engine: str,
        schema: str,
        template_processor: Any | None,
    ) -> str | None:
        if template_processor and expression:
            expression = template_processor.process_template(expression)
        if expression:
            expression = validate_adhoc_subquery(
                expression,
                self.database,
                self.catalog,
                schema,
                engine,
            )
            try:
                expression = sanitize_clause(expression, engine)
            except QueryClauseValidationException as ex:
                raise QueryObjectValidationError(ex.message) from ex
        return expression

    def _process_select_expression(
        self,
        expression: str | None,
        database_id: int,
        engine: str,
        schema: str,
        template_processor: Any | None,
    ) -> str | None:
        """Validate and process an adhoc expression used as a column or metric.

        This requires prefixing the expression with a dummy SELECT statement,
        so it can be properly parsed and validated.
        """
        if expression:
            expression = f"SELECT {expression}"

        if processed := self._process_sql_expression(
            expression=expression,
            database_id=database_id,
            engine=engine,
            schema=schema,
            template_processor=template_processor,
        ):
            prefix, expression = re.split(
                r"SELECT\s+",
                processed,
                maxsplit=1,
                flags=re.IGNORECASE,
            )
            return expression.strip()

        return None

    def _process_orderby_expression(
        self,
        expression: str | None,
        database_id: int,
        engine: str,
        schema: str,
        template_processor: Any | None,
    ) -> str | None:
        """Validate and process an ORDER BY clause expression.

        This requires prefixing the expression with a dummy SELECT statement,
        so it can be properly parsed and validated.
        """
        if expression:
            expression = f"SELECT 1 ORDER BY {expression}"

        if processed := self._process_sql_expression(
            expression=expression,
            database_id=database_id,
            engine=engine,
            schema=schema,
            template_processor=template_processor,
        ):
            prefix, expression = re.split(
                r"ORDER\s+BY",
                processed,
                maxsplit=1,
                flags=re.IGNORECASE,
            )
            return expression.strip()

        return None

    def make_sqla_column_compatible(
        self, sqla_col: ColumnElement, label: str | None = None
    ) -> ColumnElement:
        """Takes a sqlalchemy column object and adds label info if supported by engine.

        :param sqla_col: sqlalchemy column instance
        :param label: alias/label that column is expected to have
        :return: either a sqlalchemy column or label instance if supported by engine
        """
        label_expected = label or sqla_col.name
        db_engine_spec = self.db_engine_spec
        # add quotes to tables
        if db_engine_spec.get_allows_alias_in_select(self.database):
            label = db_engine_spec.make_label_compatible(label_expected)
            sqla_col = sqla_col.label(label)
        sqla_col.key = label_expected
        return sqla_col

    @staticmethod
    def _apply_cte(sql: str, cte: str | None) -> str:
        """Append a CTE before the SELECT statement if defined.

        :param sql: SELECT statement
        :param cte: CTE statement
        :return: SQL with CTE prepended
        """
        if cte:
            sql = f"{cte}\n{sql}"
        return sql

    def get_query_str_extended(
        self,
        query_obj: QueryObjectDict,
        mutate: bool = True,
    ) -> QueryStringExtended:
        sqlaq = self.get_sqla_query(**query_obj)
        sql = self.database.compile_sqla_query(
            sqlaq.sqla_query,
            catalog=self.catalog,
            schema=self.schema,
            is_virtual=bool(self.sql),
        )
        sql = self._apply_cte(sql, sqlaq.cte)

        if mutate:
            sql = self.database.mutate_sql_based_on_config(sql)
        return QueryStringExtended(
            applied_template_filters=sqlaq.applied_template_filters,
            applied_filter_columns=sqlaq.applied_filter_columns,
            rejected_filter_columns=sqlaq.rejected_filter_columns,
            labels_expected=sqlaq.labels_expected,
            prequeries=sqlaq.prequeries,
            sql=sql,
        )

    def _normalize_prequery_result_type(
        self,
        row: pd.Series,
        dimension: str,
        columns_by_name: dict[str, "TableColumn"],
    ) -> Union[str, int, float, bool, str]:
        """Convert a prequery result type to its equivalent Python type.

        Some databases like Druid will return timestamps as strings, but
        do not perform automatic casting when comparing these strings to a
        timestamp.  For cases like this we convert the value via the
        appropriate SQL transform.

        :param row: A prequery record
        :param dimension: The dimension name
        :param columns_by_name: The mapping of columns by name
        :return: equivalent primitive python type
        """

        value = row[dimension]

        if isinstance(value, np.generic):
            value = value.item()

        column_ = columns_by_name.get(dimension)
        db_extra: dict[str, Any] = self.database.get_extra()

        if column_ is None:
            # Column not found, return value as-is
            pass
        elif isinstance(column_, dict):
            if (
                column_.get("type")
                and column_.get("is_temporal")
                and isinstance(value, str)
            ):
                sql = self.db_engine_spec.convert_dttm(
                    column_.get("type"), dateutil.parser.parse(value), db_extra=None
                )

                if sql:
                    value = self.db_engine_spec.get_text_clause(sql)
        else:
            if column_.type and column_.is_temporal and isinstance(value, str):
                sql = self.db_engine_spec.convert_dttm(
                    column_.type, dateutil.parser.parse(value), db_extra=db_extra
                )

                if sql:
                    value = self.db_engine_spec.get_text_clause(sql)
        return value

    def make_orderby_compatible(
        self, select_exprs: list[ColumnElement], orderby_exprs: list[ColumnElement]
    ) -> None:
        """If needed, make sure aliases for selected columns are not used in
        ``ORDER BY``.

        In some databases (e.g. Presto), ``ORDER BY`` clause is not able to
        automatically pick the source column if a ``SELECT`` clause alias is
        named the same as a source column.  In this case, we update the SELECT
        alias to another name to avoid the conflict.
        """
        if self.db_engine_spec.allows_alias_to_source_column:
            return

        def is_alias_used_in_orderby(col: ColumnElement) -> bool:
            if not isinstance(col, Label):
                return False
            regexp = re.compile(f"\\(.*\\b{re.escape(col.name)}\\b.*\\)", re.IGNORECASE)
            return any(regexp.search(str(x)) for x in orderby_exprs)

        # Iterate through selected columns, if column alias appears in
        # orderby use another alias.  The final output columns will still
        # use the original names, because they are updated by
        # ``labels_expected`` after querying.
        for col in select_exprs:
            if is_alias_used_in_orderby(col):
                col.name = f"{col.name}__"

    def exc_query(self, qry: Any) -> QueryResult:
        from superset.utils.column import error_msg_from_exception

        qry_start_dttm = datetime.now()
        query_str_ext = self.get_query_str_extended(qry)
        sql = query_str_ext.sql
        status = "success"
        errors = None
        error_message = None

        def assign_column_label(df: pd.DataFrame) -> pd.DataFrame | None:
            """Some engines change the case or generate bespoke column names,
            either by default or due to lack of support for aliasing.  This
            function ensures that the column names in the DataFrame correspond
            to what is expected by the viz components.

            Sometimes a query may also contain only order by columns that are
            not used as metrics or groupby columns, but need to present in the
            SQL ``select``, filtering by ``labels_expected`` make sure we only
            return columns users want.

            :param df: Original DataFrame returned by the engine
            :return: Mutated DataFrame
            """
            labels_expected = query_str_ext.labels_expected
            if df is not None and not df.empty:
                if len(df.columns) < len(labels_expected):
                    raise QueryObjectValidationError(
                        "Db engine did not return all queried columns"
                    )
                if len(df.columns) > len(labels_expected):
                    df = df.iloc[:, 0 : len(labels_expected)]
                df.columns = labels_expected
            return df

        try:
            df = self.database.get_df(
                sql,
                self.catalog,
                self.schema,
                mutator=assign_column_label,
            )
        except Exception as ex:  # noqa: BLE001
            df = pd.DataFrame()
            status = "failed"
            logger.warning(
                "Query %s on schema %s failed", sql, self.schema, exc_info=True
            )
            db_engine_spec = self.db_engine_spec
            errors = [
                dataclasses.asdict(error) for error in db_engine_spec.extract_errors(ex)
            ]
            error_message = error_msg_from_exception(ex)

        return QueryResult(
            applied_template_filters=query_str_ext.applied_template_filters,
            applied_filter_columns=query_str_ext.applied_filter_columns,
            rejected_filter_columns=query_str_ext.rejected_filter_columns,
            status=status,
            df=df,
            duration=datetime.now() - qry_start_dttm,
            query=sql,
            errors=errors,
            error_message=error_message,
        )

    def get_rendered_sql(
        self,
        template_processor: Any | None = None,
    ) -> str:
        """Render sql with template engine (Jinja)."""
        if not self.sql:
            return ""

        sql = self.sql.strip("\t\r\n; ")
        if template_processor:
            try:
                sql = template_processor.process_template(sql)
            except (TemplateError, SupersetSyntaxErrorException) as ex:
                # Extract error message from different exception types
                if isinstance(ex, TemplateError):
                    error_msg = ex.message
                else:  # SupersetSyntaxErrorException
                    error_msg = str(ex.errors[0].message if ex.errors else ex)

                raise QueryObjectValidationError(
                    f"Error while rendering virtual dataset query: {error_msg}"
                ) from ex

        script = SQLScript(sql, engine=self.db_engine_spec.engine)
        if len(script.statements) > 1:
            raise QueryObjectValidationError(
                "Virtual dataset query cannot consist of multiple statements"
            )

        if not sql:
            raise QueryObjectValidationError("Virtual dataset query cannot be empty")
        return sql

    def text(self, clause: str) -> TextClause:
        return self.db_engine_spec.get_text_clause(clause)

    def get_from_clause(
        self, template_processor: Any | None = None
    ) -> tuple[Union[TableClause, Alias], str | None]:
        """Return where to select the columns and metrics from.

        Either a physical table or a virtual table with its own subquery.
        If the FROM is referencing a CTE, the CTE is returned as the second
        value in the return tuple.

        For virtual datasets, RLS filters from underlying tables are applied
        to prevent RLS bypass.
        """
        from superset.utils.rls import apply_rls

        from_sql = self.get_rendered_sql(template_processor) + "\n"
        parsed_script = SQLScript(from_sql, engine=self.db_engine_spec.engine)
        if parsed_script.has_mutation():
            raise QueryObjectValidationError("Virtual dataset query must be read-only")

        # Apply RLS filters to virtual dataset SQL to prevent RLS bypass.
        # For each table referenced in the virtual dataset, apply its RLS
        # filters.
        if parsed_script.statements:
            default_schema = self.database.get_default_schema(self.catalog)
            try:
                for statement in parsed_script.statements:
                    apply_rls(
                        self.database,
                        self.catalog,
                        self.schema or default_schema or "",
                        statement,
                    )
                # Regenerate the SQL after RLS application
                from_sql = parsed_script.format()
            except Exception as ex:  # noqa: BLE001
                # Log the error but don't fail -- RLS application is best-effort
                logger.warning("Failed to apply RLS to virtual dataset SQL: %s", ex)

        cte = self.db_engine_spec.get_cte_query(from_sql)
        from_clause: Union[TableClause, Alias] = (
            sa.table(self.db_engine_spec.cte_alias)
            if cte
            else TextAsFrom(self.text(from_sql), []).alias(VIRTUAL_TABLE_ALIAS)
        )

        return from_clause, cte

    def adhoc_metric_to_sqla(
        self,
        metric: AdhocMetric,
        columns_by_name: dict[str, "TableColumn"],
        template_processor: Any | None = None,
        processed: bool = False,
    ) -> ColumnElement:
        """Turn an adhoc metric into a sqlalchemy column.

        :param metric: Adhoc metric definition
        :param columns_by_name: Columns for the current table
        :param template_processor: template_processor instance
        :param processed: Whether the sqlExpression has already been processed
        :returns: The metric defined as a sqlalchemy column
        :rtype: sqlalchemy.sql.column
        """
        # Support both camelCase (frontend payload) and snake_case
        # (msgspec ``rename="camel"`` decoded structs).
        expression_type = metric.get("expressionType") or metric.get("expression_type")
        label = get_metric_name(metric)

        if expression_type == AdhocMetricExpressionType.SIMPLE:
            metric_column = metric.get("column") or {}
            column_name = cast(
                str,
                metric_column.get("column_name") or metric_column.get("columnName"),
            )
            sqla_column = sa.column(column_name)
            sqla_metric = self.sqla_aggregations[metric["aggregate"]](sqla_column)
        elif expression_type == AdhocMetricExpressionType.SQL:
            expression = metric.get("sqlExpression") or metric.get("sql_expression")

            if not processed:
                expression = self._process_select_expression(
                    expression=expression,
                    database_id=self.database_id,
                    engine=self.database.backend,
                    schema=self.schema,
                    template_processor=template_processor,
                )

            sqla_metric = literal_column(expression)
        else:
            raise QueryObjectValidationError("Adhoc metric expressionType is invalid")

        return self.make_sqla_column_compatible(sqla_metric, label)

    @property
    def template_params_dict(self) -> dict[Any, Any]:
        return {}

    @staticmethod
    def filter_values_handler(  # noqa: C901
        values: FilterValues | None,
        operator: str,
        target_generic_type: int,
        target_native_type: str | None = None,
        is_list_target: bool = False,
        db_engine_spec: builtins.type["BaseEngineSpec"] | None = None,
        db_extra: dict[str, Any] | None = None,
    ) -> FilterValues | None:
        from superset.typing import GenericDataType
        from superset.utils.column import cast_to_boolean, cast_to_num

        if values is None:
            return None

        def handle_single_value(
            value: FilterValue | None,
        ) -> FilterValue | None:
            if operator == FilterOperator.TEMPORAL_RANGE:
                return value
            if (
                isinstance(value, (float, int))
                and target_generic_type == GenericDataType.TEMPORAL
                and target_native_type is not None
                and db_engine_spec is not None
            ):
                value = db_engine_spec.convert_dttm(
                    target_type=target_native_type,
                    dttm=datetime.utcfromtimestamp(value / 1000),  # noqa: DTZ003
                    db_extra=db_extra,
                )
                value = literal_column(value)
            if isinstance(value, str):
                value = value.strip("\t\n")

                if target_generic_type == GenericDataType.NUMERIC and operator not in {
                    FilterOperator.ILIKE,
                    FilterOperator.LIKE,
                }:
                    # For backwards compatibility and edge cases
                    # where a column data type might have changed
                    return cast_to_num(value)
                if value == NULL_STRING:
                    return None
                if value == EMPTY_STRING:
                    return ""
            if target_generic_type == GenericDataType.BOOLEAN:
                return cast_to_boolean(value)
            return value

        if isinstance(values, (list, tuple)):
            values = [handle_single_value(v) for v in values]  # type: ignore[assignment]
        else:
            values = handle_single_value(values)  # type: ignore[assignment]
        if is_list_target and not isinstance(values, (tuple, list)):
            values = [values]  # type: ignore[assignment]
        elif not is_list_target and isinstance(values, (tuple, list)):
            values = values[0] if values else None  # type: ignore[assignment]
        return values

    def get_query_str(self, query_obj: QueryObjectDict) -> str:
        query_str_ext = self.get_query_str_extended(query_obj)
        all_queries = query_str_ext.prequeries + [query_str_ext.sql]
        return ";\n\n".join(all_queries) + ";"

    def _get_series_orderby(
        self,
        series_limit_metric: Metric,
        metrics_by_name: dict[str, "SqlMetric"],
        columns_by_name: dict[str, "TableColumn"],
        template_processor: Any | None = None,
    ) -> ColumnElement:
        if is_adhoc_metric(series_limit_metric):
            assert isinstance(series_limit_metric, dict)
            ob = self.adhoc_metric_to_sqla(series_limit_metric, columns_by_name)
        elif (
            isinstance(series_limit_metric, str)
            and series_limit_metric in metrics_by_name
        ):
            ob = metrics_by_name[series_limit_metric].get_sqla_col(
                template_processor=template_processor
            )
        else:
            raise QueryObjectValidationError(
                f"Metric '{series_limit_metric}' does not exist"
            )
        return ob

    def _reapply_query_filters(
        self,
        qry: Select,
        apply_fetch_values_predicate: bool,
        template_processor: Any | None,
        granularity: str | None,
        time_filters: list[ColumnElement],
        where_clause_and: list[ColumnElement],
        having_clause_and: list[ColumnElement],
    ) -> Select:
        """Re-apply WHERE and HAVING clauses to a reconstructed query.

        When group_others_when_limit_reached=True, the query is reconstructed
        with sa.select(), losing previously applied filters.  This method
        re-applies those filters to maintain query correctness.

        The WHERE clause includes: user filters, RLS filters, extra WHERE
        clauses, and time range filters accumulated in where_clause_and
        and time_filters.

        :param qry: The reconstructed SQLAlchemy Select object
        :param apply_fetch_values_predicate: Whether to apply fetch values predicate
        :param template_processor: Template processor for dynamic filters
        :param granularity: Time granularity (if None, time_filters not applied)
        :param time_filters: Time-based filter conditions
        :param where_clause_and: Accumulated WHERE clause conditions
        :param having_clause_and: Accumulated HAVING clause conditions
        :return: The query with filters re-applied
        """
        if apply_fetch_values_predicate and self.fetch_values_predicate:
            qry = qry.where(
                self.get_fetch_values_predicate(template_processor=template_processor)
            )

        if granularity:
            if time_filters or where_clause_and:
                qry = qry.where(and_(*(time_filters + where_clause_and)))
        else:
            all_filters = time_filters + where_clause_and
            if all_filters:
                qry = qry.where(and_(*all_filters))

        if having_clause_and:
            qry = qry.having(and_(*having_clause_and))

        return qry

    def adhoc_column_to_sqla(
        self,
        col: AdhocColumn,
        force_type_check: bool = False,
        template_processor: Any | None = None,
    ) -> ColumnElement:
        raise NotImplementedError()

    def _get_top_groups(
        self,
        df: pd.DataFrame,
        dimensions: list[str],
        groupby_exprs: dict[str, Any],
        columns_by_name: dict[str, "TableColumn"],
    ) -> ColumnElement:
        groups = []
        for _unused, row in df.iterrows():
            group = []
            for dimension in dimensions:
                value = self._normalize_prequery_result_type(
                    row,
                    dimension,
                    columns_by_name,
                )

                group.append(groupby_exprs[dimension] == value)
            groups.append(and_(*group))

        return or_(*groups)

    def _apply_series_others_grouping(
        self,
        select_exprs: list[Any],
        groupby_all_columns: dict[str, Any],
        groupby_series_columns: dict[str, Any],
        condition_factory: Callable[[str, Any], Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        """Apply "Others" grouping to series columns in both SELECT and
        GROUP BY clauses.

        This method encapsulates the common logic for replacing series
        columns with CASE expressions that group remaining series into an
        "Others" category when the series limit is reached.

        Args:
            select_exprs: List of SELECT expressions to modify
            groupby_all_columns: Dict of GROUP BY columns to modify
            groupby_series_columns: Dict of series columns to apply Others
                grouping to
            condition_factory: Function that takes (col_name, original_expr)
                and returns the condition for when to keep original value vs
                use "Others"

        Returns:
            Tuple of (modified_select_exprs, modified_groupby_all_columns)
        """
        # Modify SELECT expressions
        modified_select_exprs = []
        for expr in select_exprs:
            if hasattr(expr, "name") and expr.name in groupby_series_columns:
                # Create condition for this column using the factory function
                condition = condition_factory(expr.name, expr)

                # Create CASE expression: condition true -> original, else
                # "Others"
                case_expr = sa.case((condition, expr), else_=sa.literal("Others"))
                case_expr = self.make_sqla_column_compatible(case_expr, expr.name)
                modified_select_exprs.append(case_expr)
            else:
                modified_select_exprs.append(expr)

        # Modify GROUP BY expressions
        modified_groupby_all_columns = {}
        for col_name, gby_expr in groupby_all_columns.items():
            if col_name in groupby_series_columns:
                # Create condition for this column using the factory function
                condition = condition_factory(col_name, gby_expr)

                # Create CASE expression for groupby
                case_expr = sa.case(
                    (condition, gby_expr),
                    else_=sa.literal("Others"),
                )
                # Don't apply make_sqla_column_compatible to GROUP BY
                # expressions.  When make_sqla_column_compatible adds a
                # label to the expression, it can cause SQLAlchemy to
                # incorrectly render string literals without quotes in the
                # GROUP BY clause (e.g., "ELSE Others" instead of "ELSE
                # 'Others'")
                modified_groupby_all_columns[col_name] = case_expr
            else:
                modified_groupby_all_columns[col_name] = gby_expr

        return modified_select_exprs, modified_groupby_all_columns

    def dttm_sql_literal(self, dttm: datetime, col: "TableColumn") -> str:
        """Convert datetime object to a SQL expression string."""

        sql = (
            self.db_engine_spec.convert_dttm(col.type, dttm, db_extra=self.db_extra)
            if col.type
            else None
        )

        if sql:
            return sql

        tf = col.python_date_format

        # Fallback to the default format (if defined).
        if not tf and self.db_extra:
            tf = self.db_extra.get("python_date_format_by_column_name", {}).get(
                col.column_name
            )

        if tf:
            if tf in {"epoch_ms", "epoch_s"}:
                seconds_since_epoch = int(dttm.timestamp())
                if tf == "epoch_s":
                    return str(seconds_since_epoch)
                return str(seconds_since_epoch * 1000)
            return f"'{dttm.strftime(tf)}'"

        return f"""'{dttm.strftime("%Y-%m-%d %H:%M:%S.%f")}'"""

    def get_time_filter(
        self,
        time_col: "TableColumn",
        start_dttm: datetime | None,
        end_dttm: datetime | None,
        time_grain: str | None = None,
        label: str | None = "__time",
        template_processor: Any | None = None,
    ) -> ColumnElement:
        col = (
            time_col.get_timestamp_expression(
                time_grain=time_grain,
                label=label,
                template_processor=template_processor,
            )
            if time_grain
            else self.convert_tbl_column_to_sqla_col(
                time_col, label=label, template_processor=template_processor
            )
        )

        l = []  # noqa: E741
        if start_dttm:
            l.append(
                col
                >= self.db_engine_spec.get_text_clause(
                    self.dttm_sql_literal(start_dttm, time_col)
                )
            )
        if end_dttm:
            l.append(
                col
                < self.db_engine_spec.get_text_clause(
                    self.dttm_sql_literal(end_dttm, time_col)
                )
            )
        return and_(*l)

    def values_for_column(
        self,
        column_name: str,
        limit: int = 10000,
        denormalize_column: bool = False,
    ) -> list[Any]:
        # denormalize column name before querying for values
        # unless disabled in the dataset configuration
        db_dialect = self.database.get_dialect()
        column_name_ = (
            self.database.db_engine_spec.denormalize_name(db_dialect, column_name)
            if denormalize_column
            else column_name
        )
        cols = {col.column_name: col for col in self.columns}
        target_col = cols[column_name_]
        tp = self.get_template_processor()
        tbl, cte = self.get_from_clause(tp)

        qry = (
            # SQLAlchemy 2.0: pass column expression positionally (the
            # legacy 1.4 single-list form is removed).  The alias is
            # important because some dialects automatically add a
            # random alias to the projection due to ``DISTINCT``;
            # others uppercase column names.  This gives a
            # deterministic column name in the resulting DataFrame.
            sa.select(
                target_col.get_sqla_col(template_processor=tp).label("column_values")
            )
            .select_from(tbl)
            .distinct()
        )
        if limit:
            qry = qry.limit(limit)

        if self.fetch_values_predicate:
            qry = qry.where(self.get_fetch_values_predicate(template_processor=tp))

        rls_filters = self.get_sqla_row_level_filters(template_processor=tp)
        qry = qry.where(and_(*rls_filters))

        with self.database.get_sqla_engine() as engine:
            sql = str(qry.compile(engine, compile_kwargs={"literal_binds": True}))
            sql = self._apply_cte(sql, cte)
            sql = self.database.mutate_sql_based_on_config(sql)

            # pylint: disable=protected-access
            if engine.dialect.identifier_preparer._double_percents:
                sql = sql.replace("%%", "%")

            with engine.connect() as con:
                df = pd.read_sql_query(sql=self.text(sql), con=con)
                # replace NaN with None to ensure it can be serialized to JSON
                df = df.replace({np.nan: None})
                return df["column_values"].to_list()

    def get_timestamp_expression(
        self,
        column: dict[str, Any],
        time_grain: str | None,
        label: str | None = None,
        template_processor: Any | None = None,
    ) -> Union["TimestampExpression", Label]:
        """Return a SQLAlchemy Core element representation of self to be
        used in a query.

        :param column: column object
        :param time_grain: Optional time grain, e.g. P1Y
        :param label: alias/label that column is expected to have
        :param template_processor: template processor
        :return: A TimeExpression object wrapped in a Label if supported by db
        """
        label = label or DTTM_ALIAS
        column_spec = self.db_engine_spec.get_column_spec(column.get("type"))
        type_ = column_spec.sqla_type if column_spec else sa.DateTime
        col = sa.column(column.get("column_name"), type_=type_)

        if template_processor:
            expression = template_processor.process_template(column["column_name"])
            col = sa.literal_column(expression, type_=type_)

        time_expr = self.db_engine_spec.get_timestamp_expr(col, None, time_grain)
        return self.make_sqla_column_compatible(time_expr, label)

    def convert_tbl_column_to_sqla_col(
        self,
        tbl_column: "TableColumn",
        label: str | None = None,
        template_processor: Any | None = None,
    ) -> ColumnElement:
        label = label or tbl_column.column_name
        db_engine_spec = self.db_engine_spec
        column_spec = db_engine_spec.get_column_spec(self.type, db_extra=self.db_extra)
        type_ = column_spec.sqla_type if column_spec else None
        if expression := tbl_column.expression:
            if template_processor:
                expression = template_processor.process_template(expression)
            col = literal_column(expression, type_=type_)
        else:
            col = sa.column(tbl_column.column_name, type_=type_)
        col = self.make_sqla_column_compatible(col, label)
        return col

    def get_sqla_query(  # noqa: C901
        self,
        apply_fetch_values_predicate: bool = False,
        columns: list[ColumnTyping] | None = None,
        extras: dict[str, Any] | None = None,
        filter: list[QueryObjectFilterClause] | None = None,
        from_dttm: datetime | None = None,
        granularity: str | None = None,
        groupby: list[ColumnTyping] | None = None,
        inner_from_dttm: datetime | None = None,
        inner_to_dttm: datetime | None = None,
        is_rowcount: bool = False,
        is_timeseries: bool = True,
        metrics: list[Metric] | None = None,
        orderby: list[OrderBy] | None = None,
        order_desc: bool = True,
        to_dttm: datetime | None = None,
        series_columns: list[ColumnTyping] | None = None,
        series_limit: int | None = None,
        series_limit_metric: Metric | None = None,
        group_others_when_limit_reached: bool = False,
        row_limit: int | None = None,
        row_offset: int | None = None,
        timeseries_limit: int | None = None,
        timeseries_limit_metric: Metric | None = None,
        time_shift: str | None = None,
        rls_filters: list[Any] | None = None,
    ) -> SqlaQuery:
        """Querying any sqla table from this common interface.

        :param rls_filters: Optional caller-supplied Row-Level Security
            clauses to inject in lieu of resolving them via
            :meth:`get_sqla_row_level_filters`. Preferred form is
            ``list[ClauseElement]`` (``TextClause`` /
            ``BooleanClauseList`` returned by
            :func:`superset.utils.rls.compose_rls_where_clauses`); raw
            ``str`` fragments are still accepted for backward
            compatibility and are wrapped in :func:`sqlalchemy.text`.
            When ``None`` (default), the original *pull* path is used —
            i.e. :meth:`get_sqla_row_level_filters` is invoked to
            resolve RLS from the active session/user context. The
            *push* form is used by the async chart-data pipeline so we
            don't have to re-resolve the user inside a sync helper. It
            also eliminates a concurrency hazard that would arise if
            two async tasks shared a ``SqlaTable`` instance: each task
            now owns its kwarg-passed clause list rather than mutating
            the instance via monkey-patch.
        """
        from superset.typing import GenericDataType
        from superset.utils.date import get_since_until_from_time_range
        from superset.utils.feature_flags import feature_flag_manager

        if granularity not in self.dttm_cols and granularity is not None:
            granularity = self.main_dttm_col

        extras = extras or {}
        time_grain = extras.get("time_grain_sqla")

        # DB-specific quoting for identifiers
        with self.database.get_sqla_engine() as engine:
            quote = engine.dialect.identifier_preparer.quote

        template_kwargs: dict[str, Any] = {
            "columns": columns,
            "from_dttm": from_dttm.isoformat() if from_dttm else None,
            "groupby": groupby,
            "metrics": metrics,
            "row_limit": row_limit,
            "row_offset": row_offset,
            "time_column": granularity,
            "time_grain": time_grain,
            "to_dttm": to_dttm.isoformat() if to_dttm else None,
            "table_columns": [col.column_name for col in self.columns],
            "filter": filter,
        }
        columns = columns or []
        groupby = groupby or []
        rejected_adhoc_filters_columns: list[Union[str, ColumnTyping]] = []
        applied_adhoc_filters_columns: list[Union[str, ColumnTyping]] = []
        db_engine_spec = self.db_engine_spec
        series_column_labels = [
            db_engine_spec.make_label_compatible(column)
            for column in get_column_names(
                columns=series_columns or [],
            )
        ]
        # deprecated, to be removed in 2.0
        if is_timeseries and timeseries_limit:
            series_limit = timeseries_limit
        series_limit_metric = series_limit_metric or timeseries_limit_metric
        template_kwargs.update(self.template_params_dict)
        extra_cache_keys: list[Any] = []
        template_kwargs["extra_cache_keys"] = extra_cache_keys
        removed_filters: list[str] = []
        applied_template_filters: list[str] = []
        template_kwargs["removed_filters"] = removed_filters
        template_kwargs["applied_filters"] = applied_template_filters
        template_processor = self.get_template_processor(**template_kwargs)
        prequeries: list[str] = []
        orderby = orderby or []
        need_groupby = bool(metrics is not None or groupby)
        metrics = metrics or []

        # For backward compatibility
        if granularity not in self.dttm_cols and granularity is not None:
            granularity = self.main_dttm_col

        columns_by_name: dict[str, "TableColumn"] = {
            col.column_name: col for col in self.columns
        }
        quoted_columns_by_name = {quote(k): v for k, v in columns_by_name.items()}

        metrics_by_name: dict[str, "SqlMetric"] = {
            m.metric_name: m for m in self.metrics
        }

        if not granularity and is_timeseries:
            raise QueryObjectValidationError(
                "Datetime column not provided as part table configuration "
                "and is required by this type of chart"
            )
        if not metrics and not columns and not groupby:
            raise QueryObjectValidationError("Empty query?")

        metrics_exprs: list[ColumnElement] = []
        for metric in metrics:
            if is_adhoc_metric(metric):
                assert isinstance(metric, dict)
                metrics_exprs.append(
                    self.adhoc_metric_to_sqla(
                        metric=metric,
                        columns_by_name=columns_by_name,
                        template_processor=template_processor,
                    )
                )
            elif isinstance(metric, str) and metric in metrics_by_name:
                metrics_exprs.append(
                    metrics_by_name[metric].get_sqla_col(
                        template_processor=template_processor
                    )
                )
            else:
                raise QueryObjectValidationError(f"Metric '{metric}' does not exist")

        if metrics_exprs:
            main_metric_expr = metrics_exprs[0]
        else:
            main_metric_expr, label = literal_column("COUNT(*)"), "ccount"
            main_metric_expr = self.make_sqla_column_compatible(main_metric_expr, label)

        # To ensure correct handling of the ORDER BY labeling we need to
        # reference the metric instance if defined in the SELECT clause.
        # use the key of the ColumnClause for the expected label
        metrics_exprs_by_label = {m.key: m for m in metrics_exprs}
        metrics_exprs_by_expr = {str(m): m for m in metrics_exprs}

        # Since orderby may use adhoc metrics, too; we need to process them
        # first
        orderby_exprs: list[ColumnElement] = []
        for orig_col, ascending in orderby:  # noqa: B007
            col: Union[AdhocMetric, ColumnElement] = orig_col
            if isinstance(col, dict):
                col = cast(AdhocMetric, col)
                # Support both camelCase and snake_case payload conventions.
                sql_expr_key = (
                    "sqlExpression" if col.get("sqlExpression") else "sql_expression"
                )
                if col.get(sql_expr_key):
                    col[sql_expr_key] = self._process_orderby_expression(
                        expression=col[sql_expr_key],
                        database_id=self.database_id,
                        engine=self.database.backend,
                        schema=self.schema,
                        template_processor=template_processor,
                    )
                if is_adhoc_metric(col):
                    # add adhoc sort by column to columns_by_name if not
                    # exists
                    col = self.adhoc_metric_to_sqla(
                        col,
                        columns_by_name,
                        processed=True,
                    )
                    # use the existing instance, if possible
                    col = metrics_exprs_by_expr.get(str(col), col)
                    need_groupby = True
            elif col in metrics_exprs_by_label:
                col = metrics_exprs_by_label[col]
                need_groupby = True
            elif col in metrics_by_name:
                col = metrics_by_name[col].get_sqla_col(
                    template_processor=template_processor
                )
                need_groupby = True
            elif col in columns_by_name:
                col = self.convert_tbl_column_to_sqla_col(
                    columns_by_name[col],
                    template_processor=template_processor,
                )

            if isinstance(col, ColumnElement):
                orderby_exprs.append(col)
            else:
                # Could not convert a column reference to valid ColumnElement
                raise QueryObjectValidationError(
                    f"Unknown column used in orderby: {orig_col}"
                )

        select_exprs: list[Union[ColumnElement, Label]] = []
        groupby_all_columns: dict[str, Any] = {}
        groupby_series_columns: dict[str, Any] = {}

        # filter out the pseudo column __timestamp from columns
        columns = [col for col in columns if col != DTTM_ALIAS]
        dttm_col = columns_by_name.get(granularity) if granularity else None

        if need_groupby:
            # dedup columns while preserving order
            columns = groupby or columns
            for selected in columns:
                if isinstance(selected, str):
                    # if groupby field/expr equals granularity field/expr
                    if selected == granularity:
                        table_col = columns_by_name[selected]
                        outer = table_col.get_timestamp_expression(
                            time_grain=time_grain,
                            label=selected,
                            template_processor=template_processor,
                        )
                    # if groupby field equals a selected column
                    elif selected in columns_by_name:
                        outer = self.convert_tbl_column_to_sqla_col(
                            columns_by_name[selected],
                            template_processor=template_processor,
                        )
                    else:
                        selected = self._process_select_expression(
                            expression=selected,
                            database_id=self.database_id,
                            engine=self.database.backend,
                            schema=self.schema,
                            template_processor=template_processor,
                        )
                        outer = literal_column(f"({selected})")
                        outer = self.make_sqla_column_compatible(outer, selected)
                else:
                    outer = self.adhoc_column_to_sqla(
                        col=selected,
                        template_processor=template_processor,
                    )
                groupby_all_columns[outer.name] = outer
                if (
                    is_timeseries and not series_column_labels
                ) or outer.name in series_column_labels:
                    groupby_series_columns[outer.name] = outer
                select_exprs.append(outer)
        elif columns:
            for selected in columns:
                if is_adhoc_column(selected):
                    _sql = selected["sqlExpression"]
                    _column_label = selected["label"]
                elif isinstance(selected, str):
                    _sql = quote(selected)
                    _column_label = selected

                selected = self._process_select_expression(
                    expression=_sql,
                    database_id=self.database_id,
                    engine=self.database.backend,
                    schema=self.schema,
                    template_processor=template_processor,
                )

                select_exprs.append(
                    self.convert_tbl_column_to_sqla_col(
                        quoted_columns_by_name[selected],
                        template_processor=template_processor,
                        label=_column_label,
                    )
                    if selected in quoted_columns_by_name
                    else self.make_sqla_column_compatible(
                        literal_column(selected), _column_label
                    )
                )
            metrics_exprs = []

        time_filters: list[ColumnElement] = []

        # Process FROM clause early to populate removed_filters from
        # virtual dataset templates before we decide whether to add time
        # filters
        tbl, cte = self.get_from_clause(template_processor)

        if granularity:
            if granularity not in columns_by_name or not dttm_col:
                raise QueryObjectValidationError(
                    f'Time column "{granularity}" does not exist in dataset'
                )

            if is_timeseries:
                timestamp = dttm_col.get_timestamp_expression(
                    time_grain=time_grain,
                    template_processor=template_processor,
                )
                # always put timestamp as the first column
                select_exprs.insert(0, timestamp)
                groupby_all_columns[timestamp.name] = timestamp

            # Use main dttm column to support index with secondary dttm
            # columns.
            if (
                self.always_filter_main_dttm
                and self.main_dttm_col in self.dttm_cols
                and self.main_dttm_col != dttm_col.column_name
                and self.main_dttm_col not in removed_filters
            ):
                time_filters.append(
                    self.get_time_filter(
                        time_col=columns_by_name[self.main_dttm_col],
                        start_dttm=from_dttm,
                        end_dttm=to_dttm,
                        template_processor=template_processor,
                    )
                )

            # Check if time filter should be skipped because it was handled
            # in template.  Check both the actual column name and __timestamp
            # alias
            should_skip_time_filter = (
                dttm_col.column_name in removed_filters or DTTM_ALIAS in removed_filters
            )

            if not should_skip_time_filter:
                time_filter_column = self.get_time_filter(
                    time_col=dttm_col,
                    start_dttm=from_dttm,
                    end_dttm=to_dttm,
                    template_processor=template_processor,
                )
                time_filters.append(time_filter_column)

        # Always remove duplicates by column name, as sometimes
        # ``metrics_exprs`` can have the same name as a groupby column
        # (e.g. when users use raw columns as custom SQL adhoc metric).
        select_exprs = remove_duplicates(
            select_exprs + metrics_exprs, key=lambda x: x.name
        )

        # Expected output columns
        labels_expected = [c.key for c in select_exprs]

        # Order by columns are "hidden" columns, some databases require
        # them always be present in SELECT if an aggregation function is used
        if not db_engine_spec.allows_hidden_orderby_agg:
            select_exprs = remove_duplicates(select_exprs + orderby_exprs)

        # SQLAlchemy 2.0: pass column expressions as positional arguments,
        # not a single list (the legacy 1.4 API).  ``select_exprs`` is
        # always a list[ColumnElement] so star-unpack is safe.
        qry = sa.select(*select_exprs)

        if groupby_all_columns:
            qry = qry.group_by(*groupby_all_columns.values())

        where_clause_and: list[ColumnElement] = []
        having_clause_and: list[ColumnElement] = []

        for flt in filter:  # type: ignore[union-attr]
            if not all(flt.get(s) for s in ["col", "op"]):
                continue
            flt_col = flt["col"]
            val = flt.get("val")
            flt_grain = flt.get("grain")
            op = FilterOperator(flt["op"].upper())
            col_obj: "TableColumn | None" = None
            sqla_col: ColumnElement | None = None
            if flt_col == DTTM_ALIAS and is_timeseries and dttm_col:
                col_obj = dttm_col
            elif is_adhoc_column(flt_col):
                try:
                    sqla_col = self.adhoc_column_to_sqla(flt_col, force_type_check=True)
                    applied_adhoc_filters_columns.append(flt_col)
                except ColumnNotFoundException:
                    rejected_adhoc_filters_columns.append(flt_col)
                    continue
            else:
                col_obj = columns_by_name.get(cast(str, flt_col))
            filter_grain = flt.get("grain")

            # Check if this filter should be skipped because it was handled
            # in template.  Special handling for __timestamp alias: check both
            # the alias and the actual column name
            filter_col_name = get_column_name(flt_col)
            should_skip_filter = filter_col_name in removed_filters
            if not should_skip_filter and flt_col == DTTM_ALIAS and col_obj:
                # For __timestamp, also check if the actual datetime column
                # was removed
                should_skip_filter = col_obj.column_name in removed_filters

            if should_skip_filter:
                # Skip generating SQLA filter when the jinja template
                # handles it.
                continue

            if col_obj or sqla_col is not None:
                if sqla_col is not None:
                    pass
                elif col_obj and filter_grain:
                    sqla_col = col_obj.get_timestamp_expression(
                        time_grain=filter_grain,
                        template_processor=template_processor,
                    )
                elif col_obj:
                    sqla_col = self.convert_tbl_column_to_sqla_col(
                        tbl_column=col_obj,
                        template_processor=template_processor,
                    )
                col_type = col_obj.type if col_obj else None
                col_spec = db_engine_spec.get_column_spec(native_type=col_type)
                is_list_target = op in (
                    FilterOperator.IN,
                    FilterOperator.NOT_IN,
                )

                col_advanced_data_type = col_obj.advanced_data_type if col_obj else ""

                if col_spec and not col_advanced_data_type:
                    target_generic_type = col_spec.generic_type
                else:
                    target_generic_type = GenericDataType.STRING
                eq = self.filter_values_handler(
                    values=val,
                    operator=op,
                    target_generic_type=target_generic_type,
                    target_native_type=col_type,
                    is_list_target=is_list_target,
                    db_engine_spec=db_engine_spec,
                )

                # Get ADVANCED_DATA_TYPES from config when needed — the
                # registry lives on the Pydantic settings field
                # ``advanced_data_types``. Resolved LAZILY: constructing
                # ``SupersetSettings()`` runs the full
                # Pydantic validation chain (~10ms — os.environ scan,
                # version_info.json read), far too costly to pay per filter
                # on the hot query path; the registry is only needed when the
                # column declares an advanced data type AND the flag is on.
                uses_advanced_data_type = (
                    col_advanced_data_type != ""
                    and feature_flag_manager.is_feature_enabled(
                        "ENABLE_ADVANCED_DATA_TYPES"
                    )
                )
                ADVANCED_DATA_TYPES: dict[str, Any] = {}  # noqa: N806
                if uses_advanced_data_type:
                    from superset.config import SupersetSettings

                    ADVANCED_DATA_TYPES = (  # noqa: N806
                        SupersetSettings().advanced_data_types  # type: ignore[call-arg]
                        or {}
                    )

                if (
                    uses_advanced_data_type
                    and col_advanced_data_type in ADVANCED_DATA_TYPES
                ):
                    values = eq if is_list_target else [eq]  # type: ignore[assignment]
                    bus_resp: AdvancedDataTypeResponse = ADVANCED_DATA_TYPES[
                        col_advanced_data_type
                    ].translate_type(
                        {
                            "type": col_advanced_data_type,
                            "values": values,
                        }
                    )
                    if bus_resp["error_message"]:
                        raise AdvancedDataTypeResponseError(bus_resp["error_message"])

                    where_clause_and.append(
                        ADVANCED_DATA_TYPES[col_advanced_data_type].translate_filter(
                            sqla_col, op, bus_resp["values"]
                        )
                    )
                elif is_list_target:
                    assert isinstance(eq, (tuple, list))
                    if len(eq) == 0:
                        raise QueryObjectValidationError(
                            "Filter value list cannot be empty"
                        )
                    if len(eq) > len(
                        eq_without_none := [x for x in eq if x is not None]
                    ):
                        is_null_cond = sqla_col.is_(None)
                        if eq:
                            cond = or_(
                                is_null_cond,
                                sqla_col.in_(eq_without_none),
                            )
                        else:
                            cond = is_null_cond
                    else:
                        cond = sqla_col.in_(eq)
                    if op == FilterOperator.NOT_IN:
                        cond = ~cond
                    where_clause_and.append(cond)
                elif op in {
                    FilterOperator.IS_NULL,
                    FilterOperator.IS_NOT_NULL,
                }:
                    where_clause_and.append(
                        db_engine_spec.handle_null_filter(sqla_col, op)
                    )
                elif op == FilterOperator.IS_TRUE:
                    where_clause_and.append(
                        db_engine_spec.handle_boolean_filter(sqla_col, op, True)
                    )
                elif op == FilterOperator.IS_FALSE:
                    where_clause_and.append(
                        db_engine_spec.handle_boolean_filter(sqla_col, op, False)
                    )
                else:
                    if (
                        op
                        not in {
                            FilterOperator.EQUALS,
                            FilterOperator.NOT_EQUALS,
                        }
                        and eq is None
                    ):
                        raise QueryObjectValidationError(
                            "Must specify a value for filters with comparison operators"
                        )
                    if op in {
                        FilterOperator.EQUALS,
                        FilterOperator.NOT_EQUALS,
                        FilterOperator.GREATER_THAN,
                        FilterOperator.LESS_THAN,
                        FilterOperator.GREATER_THAN_OR_EQUALS,
                        FilterOperator.LESS_THAN_OR_EQUALS,
                    }:
                        where_clause_and.append(
                            db_engine_spec.handle_comparison_filter(sqla_col, op, eq)
                        )
                    elif op in {
                        FilterOperator.ILIKE,
                        FilterOperator.LIKE,
                    }:
                        if target_generic_type != GenericDataType.STRING:
                            sqla_col = sa.cast(sqla_col, sa.String)

                        if op == FilterOperator.LIKE:
                            where_clause_and.append(sqla_col.like(eq))
                        else:
                            where_clause_and.append(sqla_col.ilike(eq))
                    elif op in {FilterOperator.NOT_LIKE}:
                        if target_generic_type != GenericDataType.STRING:
                            sqla_col = sa.cast(sqla_col, sa.String)

                        where_clause_and.append(sqla_col.not_like(eq))
                    elif (
                        op == FilterOperator.TEMPORAL_RANGE
                        and isinstance(eq, str)
                        and col_obj is not None
                    ):
                        _since, _until = get_since_until_from_time_range(
                            time_range=eq,
                            time_shift=time_shift,
                            extras=extras,
                        )
                        where_clause_and.append(
                            self.get_time_filter(
                                time_col=col_obj,
                                start_dttm=_since,
                                end_dttm=_until,
                                time_grain=flt_grain,
                                label=sqla_col.key,
                                template_processor=template_processor,
                            )
                        )
                    else:
                        raise QueryObjectValidationError(
                            f"Invalid filter operation type: {op}"
                        )
        # ── Row-Level Security ──────────────────────────────────────
        # Two paths exist for RLS clause injection:
        #
        # 1. *Pull* (default, when ``rls_filters is None``):
        #    delegate to :meth:`get_sqla_row_level_filters` which
        #    resolves the active user via session ContextVar and
        #    composes their RLS clauses through the security manager.
        #
        # 2. *Push* (when ``rls_filters`` is provided): the caller
        #    (typically the async chart-data pipeline) has already
        #    resolved RLS up-front and supplies the ready-made
        #    ``ClauseElement`` list as a kwarg.  This avoids
        #    monkey-patching ``get_sqla_row_level_filters`` on a shared
        #    ``SqlaTable`` instance (a leakage / race hazard under
        #    concurrent ``async`` calls).
        if rls_filters is not None:
            for clause in rls_filters:
                if isinstance(clause, str):
                    stripped = clause.strip()
                    if stripped:
                        where_clause_and.append(self.text(stripped))
                else:
                    where_clause_and.append(clause)
        else:
            where_clause_and += self.get_sqla_row_level_filters(template_processor)
        if extras:
            where = extras.get("where")
            if where:
                where = self._process_select_expression(
                    expression=where,
                    database_id=self.database_id,
                    engine=self.database.backend,
                    schema=self.schema,
                    template_processor=template_processor,
                )
                where_clause_and += [self.text(where)]
            having = extras.get("having")
            if having:
                having = self._process_select_expression(
                    expression=having,
                    database_id=self.database_id,
                    engine=self.database.backend,
                    schema=self.schema,
                    template_processor=template_processor,
                )
                having_clause_and += [self.text(having)]

        if apply_fetch_values_predicate and self.fetch_values_predicate:
            qry = qry.where(
                self.get_fetch_values_predicate(template_processor=template_processor)
            )
        if granularity:
            qry = qry.where(and_(*(time_filters + where_clause_and)))
        else:
            qry = qry.where(and_(*where_clause_and))
        qry = qry.having(and_(*having_clause_and))

        self.make_orderby_compatible(select_exprs, orderby_exprs)

        for col, (_orig_col, ascending) in zip(  # noqa: B007
            orderby_exprs, orderby, strict=False
        ):
            if not db_engine_spec.allows_alias_in_orderby and isinstance(col, Label):
                # if engine does not allow using SELECT alias in ORDER BY
                # revert to the underlying column
                col = col.element

            if (
                db_engine_spec.get_allows_alias_in_select(self.database)
                and db_engine_spec.allows_hidden_cc_in_orderby
                and col.name in [select_col.name for select_col in select_exprs]
            ):
                col = literal_column(quote(col.name))
            direction = sa.asc if ascending else sa.desc
            qry = qry.order_by(direction(col))

        if row_limit:
            qry = qry.limit(row_limit)
        if row_offset:
            qry = qry.offset(row_offset)

        if series_limit and groupby_series_columns:
            if db_engine_spec.allows_joins and db_engine_spec.allows_subqueries:
                # some sql dialects require for order by expressions
                # to also be in the select clause -- others, e.g. vertica,
                # require a unique inner alias
                inner_main_metric_expr = self.make_sqla_column_compatible(
                    main_metric_expr, "mme_inner__"
                )
                inner_groupby_exprs = []
                inner_select_exprs = []
                for gby_name, gby_obj in groupby_series_columns.items():
                    inner = self.make_sqla_column_compatible(gby_obj, gby_name + "__")
                    inner_groupby_exprs.append(inner)
                    inner_select_exprs.append(inner)

                inner_select_exprs += [inner_main_metric_expr]
                subq = sa.select(*inner_select_exprs).select_from(tbl)
                inner_time_filter = []

                if dttm_col and not db_engine_spec.time_groupby_inline:
                    inner_time_filter = [
                        self.get_time_filter(
                            time_col=dttm_col,
                            start_dttm=inner_from_dttm or from_dttm,
                            end_dttm=inner_to_dttm or to_dttm,
                            template_processor=template_processor,
                        )
                    ]
                subq = subq.where(and_(*(where_clause_and + inner_time_filter)))
                subq = subq.group_by(*inner_groupby_exprs)

                ob = inner_main_metric_expr
                if series_limit_metric:
                    ob = self._get_series_orderby(
                        series_limit_metric=series_limit_metric,
                        metrics_by_name=metrics_by_name,
                        columns_by_name=columns_by_name,
                        template_processor=template_processor,
                    )
                direction = sa.desc if order_desc else sa.asc
                subq = subq.order_by(direction(ob))
                subq = subq.limit(series_limit)

                on_clause = []
                for gby_name, gby_obj in groupby_series_columns.items():
                    # in this case the column name, not the alias, needs to
                    # be conditionally mutated, as it refers to the column
                    # alias in the inner query
                    col_name = db_engine_spec.make_label_compatible(gby_name + "__")
                    on_clause.append(gby_obj == sa.column(col_name))

                # Use LEFT JOIN when grouping others, INNER JOIN otherwise
                if group_others_when_limit_reached:
                    # Create the alias once and reuse it
                    subq_alias = subq.alias(SERIES_LIMIT_SUBQ_ALIAS)
                    tbl = tbl.join(
                        subq_alias,
                        and_(*on_clause),
                        isouter=True,
                    )

                    # Apply Others grouping using the refactored method
                    def _create_join_condition(col_name: str, expr: Any) -> Any:
                        # Get the corresponding column from the subquery
                        subq_col_name = db_engine_spec.make_label_compatible(
                            col_name + "__"
                        )
                        # Reference the column from the already-created
                        # aliased subquery
                        subq_col = subq_alias.c[subq_col_name]
                        return subq_col.is_not(None)

                    select_exprs, groupby_all_columns = (
                        self._apply_series_others_grouping(
                            select_exprs,
                            groupby_all_columns,
                            groupby_series_columns,
                            _create_join_condition,
                        )
                    )

                    # Reconstruct query with modified expressions (SA2.0 API)
                    qry = sa.select(*select_exprs)
                    if groupby_all_columns:
                        qry = qry.group_by(*groupby_all_columns.values())

                    # Re-apply WHERE and HAVING clauses lost during query
                    # reconstruction
                    qry = self._reapply_query_filters(
                        qry,
                        apply_fetch_values_predicate,
                        template_processor,
                        granularity,
                        time_filters,
                        where_clause_and,
                        having_clause_and,
                    )
                else:
                    tbl = tbl.join(
                        subq.alias(SERIES_LIMIT_SUBQ_ALIAS),
                        and_(*on_clause),
                    )
            else:
                if series_limit_metric:
                    orderby = [
                        (
                            self._get_series_orderby(
                                series_limit_metric=series_limit_metric,
                                metrics_by_name=metrics_by_name,
                                columns_by_name=columns_by_name,
                                template_processor=template_processor,
                            ),
                            not order_desc,
                        )
                    ]

                # run prequery to get top groups
                prequery_obj = {
                    "is_timeseries": False,
                    "row_limit": series_limit,
                    "metrics": metrics,
                    "granularity": granularity,
                    "groupby": groupby,
                    "from_dttm": inner_from_dttm or from_dttm,
                    "to_dttm": inner_to_dttm or to_dttm,
                    "filter": filter,
                    "orderby": orderby,
                    "extras": extras,
                    "columns": get_non_base_axis_columns(columns),
                    "order_desc": True,
                }

                result = self.query(prequery_obj)
                prequeries.append(result.query)
                dimensions = [
                    c
                    for c in result.df.columns
                    if c not in metrics and c in groupby_series_columns
                ]
                top_groups = self._get_top_groups(
                    result.df,
                    dimensions,
                    groupby_series_columns,
                    columns_by_name,
                )

                if group_others_when_limit_reached:
                    # Apply Others grouping using the refactored method
                    def _create_top_groups_condition(col_name: str, expr: Any) -> Any:
                        return top_groups

                    select_exprs, groupby_all_columns = (
                        self._apply_series_others_grouping(
                            select_exprs,
                            groupby_all_columns,
                            groupby_series_columns,
                            _create_top_groups_condition,
                        )
                    )

                    # Reconstruct query with modified expressions (SA2.0 API)
                    qry = sa.select(*select_exprs)
                    if groupby_all_columns:
                        qry = qry.group_by(*groupby_all_columns.values())

                    # Re-apply WHERE and HAVING clauses lost during query
                    # reconstruction
                    qry = self._reapply_query_filters(
                        qry,
                        apply_fetch_values_predicate,
                        template_processor,
                        granularity,
                        time_filters,
                        where_clause_and,
                        having_clause_and,
                    )
                else:
                    # Original behavior: filter to only top groups
                    qry = qry.where(top_groups)

        qry = qry.select_from(tbl)

        if is_rowcount:
            if not db_engine_spec.allows_subqueries:
                raise QueryObjectValidationError("Database does not support subqueries")
            label = "rowcount"
            col = self.make_sqla_column_compatible(literal_column("COUNT(*)"), label)
            qry = sa.select(col).select_from(qry.alias("rowcount_qry"))
            labels_expected = [label]

        filter_columns = [flt.get("col") for flt in filter] if filter else []
        rejected_filter_columns = [
            col
            for col in filter_columns
            if col
            and not is_adhoc_column(col)
            and col not in self.column_names
            and col not in applied_template_filters
        ] + rejected_adhoc_filters_columns

        applied_filter_columns = [
            col
            for col in filter_columns
            if col
            and not is_adhoc_column(col)
            and (col in self.column_names or col in applied_template_filters)
        ] + applied_adhoc_filters_columns

        return SqlaQuery(
            applied_template_filters=applied_template_filters,
            cte=cte,
            applied_filter_columns=applied_filter_columns,
            rejected_filter_columns=rejected_filter_columns,
            extra_cache_keys=extra_cache_keys,
            labels_expected=labels_expected,
            sqla_query=qry,
            prequeries=prequeries,
        )
