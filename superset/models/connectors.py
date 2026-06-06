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
"""Connector models: SqlaTable, TableColumn, SqlMetric, RowLevelSecurityFilter.

Pure SQLAlchemy -- no Flask dependencies.
Includes async_query() for chart data execution via the async engine specs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Hashable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    CertificationMixin,
    ExploreMixin,
    ImportExportMixin,
    MediumText,
    metadata,
)
from superset.utils.core import (
    RowLevelSecurityFilterType as _RowLevelSecurityFilterType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _escape_sql_string(value: str) -> str:
    """Escape single quotes in a SQL literal value."""
    return value.replace("'", "''")


# SQL reserved keywords (conservative superset of ANSI SQL and common
# dialect reservations). Used by ``SqlaTable._quote_col_if_needed`` to
# decide whether a simple identifier needs quoting. Kept in sync with the
# list SQLAlchemy uses internally for default quoting behaviour.
_SQL_RESERVED_WORDS: frozenset[str] = frozenset(
    {
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "asymmetric",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "column",
        "constraint",
        "create",
        "current_catalog",
        "current_date",
        "current_role",
        "current_time",
        "current_timestamp",
        "current_user",
        "default",
        "deferrable",
        "desc",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "false",
        "fetch",
        "for",
        "foreign",
        "from",
        "grant",
        "group",
        "having",
        "in",
        "initially",
        "intersect",
        "into",
        "lateral",
        "leading",
        "limit",
        "localtime",
        "localtimestamp",
        "not",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "placing",
        "primary",
        "references",
        "returning",
        "select",
        "session_user",
        "some",
        "symmetric",
        "table",
        "then",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "user",
        "using",
        "variadic",
        "when",
        "where",
        "window",
        "with",
        # additional commonly-reserved identifiers across engines
        "date",
        "time",
        "timestamp",
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "second",
        "level",
        "number",
        "position",
        "value",
    }
)


def _parse_dttm(value: Any) -> datetime | None:
    """Coerce a datetime-ish value to a datetime object or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        if not value or value.lower() in ("", "no filter"):
            return None
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            pass
        # Try dateutil as fallback
        try:
            import dateutil.parser  # type: ignore[import-untyped]

            return dateutil.parser.parse(value)
        except (ValueError, TypeError, ImportError):
            return None
    return None


# ---------------------------------------------------------------------------
# QueryResult — return type for SqlaTable.async_query()
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    """Result of executing a query against a datasource.

    Mirrors the response payload shape exposed by the original
    Apache Superset chart-data endpoint — surfacing
    ``applied_filter_columns``, ``rejected_filter_columns``,
    ``applied_template_filters``, ``labels_expected`` and
    ``prequeries`` so downstream consumers (cache keys, dashboards
    UX) get the same metadata they used to.
    """

    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    query: str = ""
    status: str = "success"
    error_message: str = ""
    from_dttm: datetime | None = None
    to_dttm: datetime | None = None
    applied_filter_columns: list[Any] = field(default_factory=list)
    rejected_filter_columns: list[Any] = field(default_factory=list)
    applied_template_filters: list[str] = field(default_factory=list)
    labels_expected: list[str] = field(default_factory=list)
    prequeries: list[str] = field(default_factory=list)


@dataclass
class MetadataResult:
    """Diff returned by ``fetch_metadata`` — 1:1 with the original
    ``superset_old/connectors/sqla/models.py:131``: the column names added,
    removed and (type-)modified during an introspection refresh.
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------

sqlatable_user = Table(
    "sqlatable_user",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "user_id",
        Integer,
        ForeignKey("ab_user.id", ondelete="CASCADE"),
    ),
    Column(
        "table_id",
        Integer,
        ForeignKey("tables.id", ondelete="CASCADE"),
    ),
)

# M2M tables — ported 1:1 from
# ``superset_old/connectors/sqla/models.py``: no ``ondelete=CASCADE`` (the
# original Superset migrations don't define one), and ``role_id`` is
# ``nullable=False``.
RLSFilterRoles = Table(
    "rls_filter_roles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("role_id", Integer, ForeignKey("ab_role.id"), nullable=False),
    Column("rls_filter_id", Integer, ForeignKey("row_level_security_filters.id")),
)

RLSFilterTables = Table(
    "rls_filter_tables",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("table_id", Integer, ForeignKey("tables.id")),
    Column("rls_filter_id", Integer, ForeignKey("row_level_security_filters.id")),
)


# ---------------------------------------------------------------------------
# BaseDatasource mixin
# ---------------------------------------------------------------------------


class BaseDatasource:
    """Common columns shared by all datasource-like models.

    This is an abstract mixin -- not a mapped class itself.
    """

    # Used to do code highlighting when displaying the query in the UI
    query_language: str | None = None

    # Only some datasources support Row Level Security
    is_rls_supported: bool = False

    description = Column(Text)
    default_endpoint = Column(Text)
    is_featured = Column(Boolean, default=False)
    filter_select_enabled = Column(Boolean, default=True)
    offset = Column(Integer, default=0)
    cache_timeout = Column(Integer)
    params = Column(String(1000))
    perm = Column(String(1000))
    schema_perm = Column(String(1000))
    catalog_perm = Column(String(1000))
    is_managed_externally = Column(Boolean, nullable=False, default=False)
    external_url = Column(Text, nullable=True)

    def get_extra_cache_keys(
        self,
        query_obj: dict[str, Any],  # noqa: ARG002
    ) -> list[Hashable]:
        """If a datasource needs to provide additional keys for calculation of
        cache keys, those can be provided via this method.

        1:1 with ``BaseDatasource.get_extra_cache_keys`` in
        ``superset_old/connectors/sqla/models.py`` (line 609).

        :param query_obj: The dict representation of a query object
        :return: list of keys
        """
        return []


# ---------------------------------------------------------------------------
# TableColumn
# ---------------------------------------------------------------------------


class TableColumn(AuditMixinNullable, ImportExportMixin, CertificationMixin, Base):
    """A column belonging to a SQL dataset (SqlaTable)."""

    __tablename__ = "table_columns"
    __table_args__ = (UniqueConstraint("table_id", "column_name"),)

    id = Column(Integer, primary_key=True)
    column_name = Column(String(255), nullable=False)
    verbose_name = Column(String(1024))
    is_active = Column(Boolean, default=True)
    type = Column(Text)
    advanced_data_type = Column(String(255))
    groupby = Column(Boolean, default=True)
    filterable = Column(Boolean, default=True)
    description = Column(MediumText())
    table_id = Column(
        Integer,
        ForeignKey("tables.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_dttm = Column(Boolean, default=False)
    expression = Column(MediumText())
    python_date_format = Column(String(255))
    extra = Column(Text)

    # -- relationships --------------------------------------------------------

    table = relationship(
        "SqlaTable",
        foreign_keys=[table_id],
        back_populates="columns",
    )

    export_fields = [
        "table_id",
        "column_name",
        "verbose_name",
        "is_dttm",
        "is_active",
        "type",
        "advanced_data_type",
        "groupby",
        "filterable",
        "expression",
        "description",
        "python_date_format",
        "extra",
    ]
    update_from_object_fields = [s for s in export_fields if s not in ("table_id",)]
    export_parent = "table"

    # -- Computed properties ---------------------------------------------------

    _NUMERIC_TYPES = frozenset(
        {
            "INT",
            "INTEGER",
            "BIGINT",
            "SMALLINT",
            "TINYINT",
            "FLOAT",
            "DOUBLE",
            "DECIMAL",
            "NUMERIC",
            "REAL",
            "DOUBLE PRECISION",
            "MONEY",
            "NUMBER",
        }
    )

    @property
    def is_numeric(self) -> bool:
        """Check if the column has a numeric datatype."""
        if self.type is None:
            return False
        base = self.type.split("(")[0].strip().upper()
        return base in self._NUMERIC_TYPES

    @property
    def type_generic(self) -> int | None:
        from superset.typing import GenericDataType

        if self.is_dttm:
            return int(GenericDataType.TEMPORAL)
        table = getattr(self, "table", None)
        database = getattr(table, "database", None)
        if database is None:
            return None
        try:
            db_extra = database.get_extra()
        except Exception:  # noqa: BLE001
            db_extra = None
        try:
            column_spec = database.db_engine_spec.get_column_spec(
                self.type, db_extra=db_extra
            )
        except Exception:  # noqa: BLE001
            return None
        if column_spec is None:
            return None
        return int(column_spec.generic_type)

    @property
    def data(self) -> dict[str, Any]:
        """Data representation sent to the frontend."""
        attrs = (
            "advanced_data_type",
            "certification_details",
            "certified_by",
            "column_name",
            "description",
            "expression",
            "filterable",
            "groupby",
            "id",
            "is_dttm",
            "python_date_format",
            "type",
            "verbose_name",
        )
        return {s: getattr(self, s) for s in attrs if hasattr(self, s)}

    # -- AST hooks used by helpers.ExploreMixin.get_sqla_query ---------------

    @property
    def database(self) -> Any:
        """Return the parent dataset's database, or None."""
        table = getattr(self, "table", None)
        return getattr(table, "database", None) if table is not None else None

    @property
    def db_engine_spec(self) -> Any:
        """Return the parent dataset's engine spec."""
        db = self.database
        return db.db_engine_spec if db is not None else None

    @property
    def db_extra(self) -> dict[str, Any] | None:
        """Return the parent dataset's database extra JSON dict."""
        db = self.database
        if db is not None and hasattr(db, "get_extra"):
            try:
                return db.get_extra()
            except Exception:  # noqa: BLE001
                return None
        return None

    @property
    def is_temporal(self) -> bool:
        """True if this column represents a datetime.

        1:1 with ``TableColumn.is_temporal`` in
        ``superset_old/connectors/sqla/models.py``
        (line 848): tri-state semantics — when ``is_dttm`` has been
        explicitly set (``True`` or ``False``) we honour it, otherwise
        we fall through to the engine spec's column-spec resolution.
        This is materially different from a two-state ``bool(is_dttm)``
        check because users can opt a column *out* of being temporal
        even when the database type would suggest otherwise (e.g.
        manual override on a numeric epoch column with
        ``python_date_format`` set elsewhere).
        """
        if self.is_dttm is not None:
            return bool(self.is_dttm)
        spec = self.db_engine_spec
        if spec is None or not self.type:
            return False
        try:
            column_spec = spec.get_column_spec(self.type, db_extra=self.db_extra)
        except Exception:  # noqa: BLE001
            return False
        return bool(column_spec and column_spec.is_dttm)

    def get_sqla_col(
        self,
        label: str | None = None,
        template_processor: Any | None = None,
    ) -> Any:
        """Return a SQLAlchemy ``ColumnElement`` for this column.

        1:1 with ``TableColumn.get_sqla_col`` in
        ``superset_old/connectors/sqla/models.py``
        (line 887). Honours calculated columns (``self.expression``)
        with optional Jinja templating, and applies engine-spec label
        compatibility via ``Database.make_sqla_column_compatible``.
        """
        from sqlalchemy import column as sa_column
        from sqlalchemy.sql import literal_column

        from superset.exceptions import (
            QueryObjectValidationError,
            SupersetSyntaxErrorException,
        )

        label = label or self.column_name
        spec = self.db_engine_spec
        type_ = None
        if spec is not None and self.type:
            try:
                column_spec = spec.get_column_spec(self.type, db_extra=self.db_extra)
                type_ = column_spec.sqla_type if column_spec else None
            except Exception:  # noqa: BLE001
                type_ = None

        expression = self.expression
        if expression:
            if template_processor:
                try:
                    expression = template_processor.process_template(expression)
                except SupersetSyntaxErrorException as ex:
                    raise QueryObjectValidationError(
                        f"Error in jinja expression in column expression: {ex}"
                    ) from ex
            col = literal_column(expression, type_=type_)
        else:
            col = sa_column(self.column_name, type_=type_)

        db = self.database
        if db is not None:
            return db.make_sqla_column_compatible(col, label)
        return col

    def get_timestamp_expression(
        self,
        time_grain: str | None,
        label: str | None = None,
        template_processor: Any | None = None,
    ) -> Any:
        """Return time-grain-truncated timestamp expression.

        1:1 with ``TableColumn.get_timestamp_expression`` in
        ``superset_old/connectors/sqla/models.py``
        (line 918). Builds a ``TimestampExpression`` AST node via the
        engine spec's ``get_timestamp_expr`` for time-bucketing of
        datetime columns; supports epoch-stored columns via
        ``python_date_format``.
        """
        from sqlalchemy import column as sa_column, DateTime
        from sqlalchemy.sql import literal_column

        from superset.exceptions import (
            QueryObjectValidationError,
            SupersetSyntaxErrorException,
        )
        from superset.models.helpers import DTTM_ALIAS

        label = label or DTTM_ALIAS
        pdf = self.python_date_format
        is_epoch = pdf in ("epoch_s", "epoch_ms")
        spec = self.db_engine_spec
        type_ = DateTime
        if spec is not None and self.type:
            try:
                column_spec = spec.get_column_spec(self.type, db_extra=self.db_extra)
                type_ = column_spec.sqla_type if column_spec else DateTime
            except Exception:  # noqa: BLE001
                type_ = DateTime

        if not self.expression and not time_grain and not is_epoch:
            sqla_col = sa_column(self.column_name, type_=type_)
            db = self.database
            if db is not None:
                return db.make_sqla_column_compatible(sqla_col, label)
            return sqla_col

        expression = self.expression
        if expression:
            if template_processor:
                try:
                    expression = template_processor.process_template(expression)
                except SupersetSyntaxErrorException as ex:
                    raise QueryObjectValidationError(
                        f"Error in jinja expression in datetime column: {ex}"
                    ) from ex
            col = literal_column(expression, type_=type_)
        else:
            col = sa_column(self.column_name, type_=type_)

        time_expr = spec.get_timestamp_expr(col, pdf, time_grain)
        db = self.database
        if db is not None:
            return db.make_sqla_column_compatible(time_expr, label)
        return time_expr


# ---------------------------------------------------------------------------
# SqlMetric
# ---------------------------------------------------------------------------


class SqlMetric(AuditMixinNullable, ImportExportMixin, CertificationMixin, Base):
    """A metric defined on a SQL dataset."""

    __tablename__ = "sql_metrics"
    __table_args__ = (UniqueConstraint("table_id", "metric_name"),)

    id = Column(Integer, primary_key=True)
    metric_name = Column(String(255), nullable=False)
    verbose_name = Column(String(1024))
    metric_type = Column(String(32))
    description = Column(MediumText())
    d3format = Column(String(128))
    currency = Column(JSON, nullable=True)
    warning_text = Column(Text)
    table_id = Column(
        Integer,
        ForeignKey("tables.id", ondelete="CASCADE"),
        nullable=False,
    )
    expression = Column(MediumText(), nullable=False)
    extra = Column(Text)

    # -- relationships --------------------------------------------------------

    table = relationship(
        "SqlaTable",
        foreign_keys=[table_id],
        back_populates="metrics",
    )

    export_fields = [
        "metric_name",
        "verbose_name",
        "metric_type",
        "table_id",
        "expression",
        "description",
        "d3format",
        "currency",
        "extra",
        "warning_text",
    ]
    update_from_object_fields = [s for s in export_fields if s != "table_id"]
    export_parent = "table"

    # -- Computed properties ---------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        """Data representation sent to the frontend."""
        attrs = (
            "certification_details",
            "certified_by",
            "currency",
            "d3format",
            "description",
            "expression",
            "id",
            "metric_name",
            "warning_text",
            "verbose_name",
        )
        return {s: getattr(self, s) for s in attrs}

    # -- AST hook used by helpers.ExploreMixin.get_sqla_query ---------------

    def get_sqla_col(
        self,
        label: str | None = None,
        template_processor: Any | None = None,
    ) -> Any:
        """Return a SQLAlchemy ``ColumnElement`` for this metric.

        1:1 with ``SqlMetric.get_sqla_col`` in
        ``superset_old/connectors/sqla/models.py``
        (line 1027). Honours optional Jinja templating of the metric
        expression and applies engine-spec label compatibility.
        """
        from sqlalchemy.sql import literal_column

        from superset.exceptions import (
            QueryObjectValidationError,
            SupersetSyntaxErrorException,
        )

        label = label or self.metric_name
        expression = self.expression
        # 1:1 with original — Jinja-process the expression even when
        # it is empty / falsy. The original at
        # ``superset_old/connectors/sqla/models.py:SqlMetric.get_sqla_col``
        # only gates on ``template_processor`` being supplied; gating
        # on ``expression`` would skip Jinja for legitimately empty
        # macros that resolve to non-empty SQL post-processing.
        if template_processor:
            try:
                expression = template_processor.process_template(expression)
            except SupersetSyntaxErrorException as ex:
                raise QueryObjectValidationError(
                    f"Error in jinja expression in metric expression: {ex}"
                ) from ex

        sqla_col = literal_column(expression)
        # 1:1 with original — let the AttributeError bubble when
        # ``self.table`` or ``self.table.database`` is missing.
        # Silently returning the raw column would skip the
        # engine-spec ``make_label_compatible`` step (Oracle 30-char
        # truncation / MSSQL bracketed alias), which produces invalid
        # SQL on those dialects rather than a loud failure here.
        return self.table.database.make_sqla_column_compatible(sqla_col, label)


# ---------------------------------------------------------------------------
# AsyncQueryExecutionMixin
# ---------------------------------------------------------------------------


class AsyncQueryExecutionMixin:
    """Datasource-agnostic async SQL build/execution helpers.

    These methods were originally defined directly on :class:`SqlaTable`
    but only reference the generic datasource surface (``self.database`` /
    ``self.sql`` / ``self.catalog`` / ``self.schema`` / ``self.db_engine_spec``)
    plus :meth:`ExploreMixin.get_sqla_query`. Extracting them into a mixin
    lets a SQL Lab :class:`~superset.models.sql_lab.Query` (which also mixes
    in :class:`ExploreMixin`) act as a chart datasource — exactly the
    ``datasource_type="query"`` capability upstream gives ``Query`` via the
    same ``ExploreMixin`` interface.

    The refactor is behaviour-preserving for ``SqlaTable``: none of these
    method names are defined on any other base in ``SqlaTable``'s MRO, so
    resolution is unchanged.
    """

    def _adapt_query_dict_for_get_sqla_query(
        self, query_dict: dict[str, Any]
    ) -> dict[str, Any]:
        """Map QueryContext-style ``query_dict`` keys → ``get_sqla_query`` kwargs.

        The wire format used by ``QueryContext.to_dict`` (and propagated
        through the chart-data pipeline) uses snake_case names like
        ``filter`` (not ``filters``) and lumps temporal extras into
        ``extras``. The :meth:`helpers.ExploreMixin.get_sqla_query`
        signature is the original Superset 1:1 contract — see
        ``superset_old/models/helpers.py:1653`` — so we translate keys
        here.
        """
        # Build the kwargs dict, accepting both ``filter`` and ``filters``
        # for compatibility with both the wire format and dataclasses
        # that prefer the plural name.
        adapted: dict[str, Any] = {
            "apply_fetch_values_predicate": query_dict.get(
                "apply_fetch_values_predicate", False
            ),
            "columns": query_dict.get("columns") or [],
            "extras": query_dict.get("extras") or {},
            "filter": query_dict.get("filter") or query_dict.get("filters") or [],
            "from_dttm": _parse_dttm(query_dict.get("from_dttm")),
            "granularity": query_dict.get("granularity"),
            "groupby": query_dict.get("groupby") or [],
            "inner_from_dttm": _parse_dttm(query_dict.get("inner_from_dttm")),
            "inner_to_dttm": _parse_dttm(query_dict.get("inner_to_dttm")),
            "is_rowcount": query_dict.get("is_rowcount", False),
            "is_timeseries": query_dict.get("is_timeseries", False),
            "metrics": query_dict.get("metrics"),
            "orderby": query_dict.get("orderby") or [],
            "order_desc": query_dict.get("order_desc", True),
            "to_dttm": _parse_dttm(query_dict.get("to_dttm")),
            "series_columns": query_dict.get("series_columns") or [],
            "series_limit": query_dict.get("series_limit"),
            "series_limit_metric": query_dict.get("series_limit_metric"),
            "group_others_when_limit_reached": query_dict.get(
                "group_others_when_limit_reached", False
            ),
            "row_limit": query_dict.get("row_limit"),
            "row_offset": query_dict.get("row_offset"),
            "timeseries_limit": query_dict.get("timeseries_limit"),
            "timeseries_limit_metric": query_dict.get("timeseries_limit_metric"),
            "time_shift": query_dict.get("time_shift"),
        }
        return adapted

    def _get_sqla_query_with_rls(
        self,
        query_dict: dict[str, Any],
        rls_filters: list[Any] | None = None,
    ) -> Any:
        """Run :meth:`get_sqla_query` honouring caller-supplied RLS clauses.

        The original chart-data pipeline computes the user's RLS
        predicates inside :meth:`get_sqla_row_level_filters` (sync)
        which reads from the active session/user context. The Liteset
        async pipeline computes RLS up-front via
        :func:`superset.utils.rls.compose_rls_where_clauses` and pushes
        it down so we don't have to re-resolve the user inside a sync
        helper.

        Implementation: we forward ``rls_filters`` as a *kwarg* to
        :meth:`helpers.ExploreMixin.get_sqla_query`. That method
        consumes the kwarg and either uses the supplied clauses (push
        path, used by async controllers) or falls back to the regular
        :meth:`get_sqla_row_level_filters` pull path. This avoids the
        monkey-patch race-condition that the previous implementation
        exhibited — concurrent ``async`` tasks running
        ``_build_sql`` / ``async_query`` on the same shared
        ``SqlaTable`` instance now never mutate instance attributes,
        so each task's RLS clauses stay private to its own call.
        """
        adapted = self._adapt_query_dict_for_get_sqla_query(query_dict)
        if rls_filters is not None:
            adapted["rls_filters"] = rls_filters
        return self.get_sqla_query(**adapted)

    def _build_sql(  # noqa: C901
        self,
        query_dict: dict[str, Any],
        rls_filters: list[Any] | None = None,
    ) -> tuple[str, datetime | None, datetime | None]:
        """Build a SQL string from query_dict parameters.

        Strategy A — thin wrapper over
        :meth:`helpers.ExploreMixin.get_sqla_query` which is a 1:1
        port of the original Apache Superset AST pipeline at
        ``superset_old/models/helpers.py:1653-2347``. The wrapper:

        1. Adapts the QueryContext wire-format keys to
           ``get_sqla_query`` kwargs.
        2. Injects caller-supplied RLS clauses (preferred form
           ``ClauseElement``, backward-compat ``str``).
        3. Compiles the AST via :meth:`Database.compile_sqla_query`
           (handles literal-bind, dialect-specific identifier quoting
           and the ``%%`` double-percent fixup).
        4. Hoists any CTE the engine spec required to live above the
           SELECT (MSSQL / Ocient).
        5. Applies ``SQL_QUERY_MUTATOR`` via
           :meth:`Database.mutate_sql_based_on_config`.

        All 19 behavioural deltas (mixed-NULL IN, time filters via
        ``convert_dttm`` / ``python_date_format``, ``time_grain`` on
        filters / ORDER BY, ``is_rowcount``,
        ``group_others_when_limit_reached``, ``apply_fetch_values_predicate``,
        ``time_shift``, ``always_filter_main_dttm``,
        ``make_orderby_compatible``, ``allows_alias_in_orderby`` /
        ``allows_hidden_cc_in_orderby`` / ``allows_hidden_orderby_agg``,
        ``make_label_compatible``, ``extras.where`` / ``extras.having``
        Jinja, ``extra_cache_keys``, virtual-dataset RLS injection)
        are handled inside :meth:`get_sqla_query` itself, mirroring
        the original.

        Returns ``(sql, from_dttm, to_dttm)``.
        """
        sqla_query = self._get_sqla_query_with_rls(query_dict, rls_filters=rls_filters)

        sql = self.database.compile_sqla_query(
            sqla_query.sqla_query,
            catalog=self.catalog,
            schema=self.schema,
            is_virtual=bool(self.sql),
        )
        sql = self._apply_cte(sql, sqla_query.cte)
        sql = self.database.mutate_sql_based_on_config(sql)

        return (
            sql,
            _parse_dttm(query_dict.get("from_dttm")),
            _parse_dttm(query_dict.get("to_dttm")),
        )

    def _build_from_ast(self) -> tuple[Any, str | None]:
        """Return ``(from_clause, cte_sql)`` as a SQLAlchemy AST node.

        Mirrors the original ``ExploreMixin.get_from_clause``
        (``superset_old/models/helpers.py:1163``) but returns a
        SQLAlchemy ``FromClause`` rather than a ``str`` so it can be
        passed directly to ``sa.select(...).select_from(...)``.

        - Physical dataset → ``sa.table(table_name, schema=...)``
          (catalog is folded into the schema for engines that need it).
        - Virtual dataset on an engine that supports CTEs in
          subqueries → ``TextAsFrom(user_sql).alias("virtual_table")``.
        - Virtual dataset on an engine that does *not* support CTEs in
          subqueries (e.g. MSSQL/Ocient) → ``sa.table(cte_alias)`` plus
          a ``cte_sql`` string the caller hoists above the SELECT.
        """
        from sqlalchemy.sql.expression import TextAsFrom

        if not self.sql:
            # Physical dataset — build a real Table-like FROM node.
            schema_name = self.schema or None
            if self.catalog:
                # Some engines pack catalog.schema into the schema; mirror
                # the string-builder behaviour by joining them so the
                # dialect emits ``catalog.schema.table``.
                schema_name = (
                    f"{self.catalog}.{schema_name}"
                    if schema_name
                    else str(self.catalog)
                )
            return sa.table(str(self.table_name), schema=schema_name), None

        # Virtual dataset
        inner = self.sql.strip().rstrip(";")
        engine_spec = self.database.db_engine_spec
        cte: str | None = None
        try:
            cte = engine_spec.get_cte_query(inner)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to detect CTE in virtual dataset SQL for table %s",
                getattr(self, "table_name", None),
                exc_info=True,
            )
            cte = None

        if cte:
            # Engine spec rewrote the user SQL into a top-level CTE; the
            # FROM references the CTE alias, and the caller prepends
            # ``cte`` above the rendered SELECT.
            return sa.table(engine_spec.cte_alias), cte

        # Default virtual handling: wrap user SQL as a derived table
        # alias, exactly matching original ``get_from_clause``.
        return TextAsFrom(sa.text(inner), []).alias("virtual_table"), None

    async def _execute_sql(self, sql: str) -> pd.DataFrame:
        """Execute SQL against the dataset's database and return a DataFrame."""
        from sqlalchemy.sql import text as sa_text

        from superset.utils.database import get_async_connection

        async with get_async_connection(self.database) as (conn, _spec):
            result = await conn.execute(sa_text(sql))
            if result.returns_rows:
                cols = list(result.keys())
                rows = result.fetchall()
                return pd.DataFrame([tuple(row) for row in rows], columns=cols)
            return pd.DataFrame()

    async def async_query(
        self,
        query_dict: dict[str, Any],
        rls_filters: list[Any] | None = None,
    ) -> QueryResult:
        """Execute a query against this dataset and return a QueryResult.

        This is the primary entry point for the async query pipeline used by
        AsyncQueryContextProcessor._get_query_result().

        Surfaces all metadata produced by
        :meth:`helpers.ExploreMixin.get_sqla_query` — namely
        ``applied_filter_columns``, ``rejected_filter_columns``,
        ``applied_template_filters``, ``labels_expected`` and
        ``prequeries`` — so the chart-data response payload matches the
        original Apache Superset shape (delta #19).

        Args:
            query_dict: The query parameters dict from QueryContext.
            rls_filters: Optional list of Row-Level Security clauses
                (``ClauseElement`` preferred — ``TextClause`` /
                ``BooleanClauseList``; raw ``str`` fragments still
                accepted for backward compat).  See ``_build_sql`` for
                the dialect-compile pipeline.  The caller is responsible
                for obtaining these from the security manager.
        """
        try:
            # SQL building below is sync and CPU/IO heavy:
            #   * ``_get_sqla_query_with_rls`` runs Jinja templating and
            #     SQLAlchemy AST construction (and may trigger
            #     ``_probe_adhoc_column_is_dttm`` which opens a sync
            #     DB-API connection).
            #   * ``compile_sqla_query`` enters a sync engine context and
            #     compiles the AST to a dialect string (and runs the
            #     SQLGlot OPTIMIZE_SQL pass for virtual datasets).
            #   * ``mutate_sql_based_on_config`` dispatches the
            #     user-defined ``SQL_QUERY_MUTATOR`` callable, which is
            #     also sync.
            #
            # Running them directly inside this coroutine would block the
            # event loop. Hand them off to a worker thread via
            # :func:`asyncio.to_thread`, which on Python >=3.9 copies the
            # current :class:`~contextvars.Context` (verified for 3.12 in
            # CPython's stdlib) so :func:`get_current_user` and friends
            # still resolve to the same caller-bound user inside the
            # worker thread.
            sqla_query = await asyncio.to_thread(
                self._get_sqla_query_with_rls,
                query_dict,
                rls_filters,
            )
            sql = await asyncio.to_thread(
                self.database.compile_sqla_query,
                sqla_query.sqla_query,
                self.catalog,
                self.schema,
                bool(self.sql),
            )
            sql = self._apply_cte(sql, sqla_query.cte)
            sql = await asyncio.to_thread(self.database.mutate_sql_based_on_config, sql)
            logger.debug("async_query SQL:\n%s", sql)

            from_dttm = _parse_dttm(query_dict.get("from_dttm"))
            to_dttm = _parse_dttm(query_dict.get("to_dttm"))

            df = await self._execute_sql(sql)

            # 1:1 with original
            # ``superset_old/connectors/sqla/models.py:assign_column_label``
            # (line 1626) — dialects like MSSQL / Snowflake may rename
            # or change the case of columns. The expected behaviour is:
            #
            # 1. If the engine returned *fewer* columns than the
            #    helpers-mixin counted (``labels_expected``), the
            #    query is malformed; raise a validation error rather
            #    than silently truncating user output.
            # 2. If the engine returned *more* columns (e.g. an
            #    ORDER BY column not in SELECT was returned anyway),
            #    keep only the leading ``labels_expected`` columns —
            #    this matches the ``df.iloc[:, 0:len(labels_expected)]``
            #    slice in the original.
            # 3. Reassign ``df.columns`` to the canonical labels so
            #    downstream viz components find the column names they
            #    expect.
            if not df.empty and sqla_query.labels_expected:
                from superset.exceptions import QueryObjectValidationError

                expected = list(sqla_query.labels_expected)
                if len(df.columns) < len(expected):
                    raise QueryObjectValidationError(
                        "Db engine did not return all queried columns"
                    )
                if len(df.columns) > len(expected):
                    df = df.iloc[:, 0 : len(expected)]
                df.columns = expected

            return QueryResult(
                df=df,
                query=sql,
                status="success",
                from_dttm=from_dttm,
                to_dttm=to_dttm,
                applied_filter_columns=list(sqla_query.applied_filter_columns),
                rejected_filter_columns=list(sqla_query.rejected_filter_columns),
                applied_template_filters=list(sqla_query.applied_template_filters),
                labels_expected=list(sqla_query.labels_expected),
                prequeries=list(sqla_query.prequeries),
            )
        except Exception as ex:
            logger.warning(
                "async_query failed for datasource %s: %s",
                getattr(self, "table_name", None),
                ex,
                exc_info=True,
            )
            return QueryResult(
                df=pd.DataFrame(),
                query="",
                status="error",
                error_message=str(ex),
            )


# ---------------------------------------------------------------------------
# SqlaTable
# ---------------------------------------------------------------------------


class SqlaTable(
    Base,
    AuditMixinNullable,
    ImportExportMixin,
    BaseDatasource,
    AsyncQueryExecutionMixin,
    ExploreMixin,
):
    """A SQL dataset (table or virtual query)."""

    type = "table"
    query_language = "sql"
    is_rls_supported = True

    __tablename__ = "tables"
    __table_args__ = (
        UniqueConstraint("database_id", "catalog", "schema", "table_name"),
    )

    id = Column(Integer, primary_key=True)
    table_name = Column(String(250), nullable=False)
    main_dttm_col = Column(String(250))
    database_id = Column(Integer, ForeignKey("dbs.id"), nullable=False)
    fetch_values_predicate = Column(Text)
    schema = Column(String(255))
    catalog = Column(String(256), nullable=True, default=None)
    sql = Column(MediumText())
    is_sqllab_view = Column(Boolean, default=False)
    template_params = Column(Text)
    # 1:1 with superset_old/connectors/sqla/models.py: SqlaTable.extra has no
    # Python default (stays NULL), unlike Database.extra which has a template.
    extra = Column(Text)
    normalize_columns = Column(Boolean, default=False)
    always_filter_main_dttm = Column(Boolean, default=False)
    folders = Column(JSON, nullable=True)

    # -- relationships --------------------------------------------------------

    columns = relationship(
        "TableColumn",
        back_populates="table",
        cascade="all, delete-orphan",
    )
    metrics = relationship(
        "SqlMetric",
        back_populates="table",
        cascade="all, delete-orphan",
    )
    owners = relationship(
        "User",
        secondary=sqlatable_user,
        passive_deletes=True,
    )
    database = relationship(
        "Database",
        foreign_keys=[database_id],
    )

    export_fields = [
        "table_name",
        "main_dttm_col",
        "description",
        "default_endpoint",
        "database_id",
        "offset",
        "cache_timeout",
        "catalog",
        "schema",
        "sql",
        "params",
        "template_params",
        "filter_select_enabled",
        "fetch_values_predicate",
        "extra",
        "normalize_columns",
        "always_filter_main_dttm",
        "folders",
    ]
    update_from_object_fields = [f for f in export_fields if f != "database_id"]
    export_parent = "database"
    export_children = ["metrics", "columns"]

    # -- DatasourceProtocol implementation ------------------------------------

    #: Datasource type identifier used by QueryContext
    type: str = "table"

    #: Aggregation functions mapping for SIMPLE adhoc metrics.
    # Function names are lowercase to match the original SQLAlchemy output
    # (``sa.func.SUM(col)`` compiles to ``sum(col)``), which Cypress tests
    # rely on.
    _SQLA_AGGREGATIONS: dict[str, str] = {
        "COUNT_DISTINCT": "COUNT(DISTINCT {col})",
        "COUNT": "COUNT({col})",
        "SUM": "sum({col})",
        "AVG": "avg({col})",
        "MIN": "min({col})",
        "MAX": "max({col})",
    }

    #: Simple filter operator mapping
    _FILTER_OPS: dict[str, str] = {
        "==": "=",
        "!=": "!=",
        ">": ">",
        "<": "<",
        ">=": ">=",
        "<=": "<=",
        "LIKE": "LIKE",
        "ILIKE": "ILIKE",
        "IS NULL": "IS NULL",
        "IS NOT NULL": "IS NOT NULL",
        "IN": "IN",
        "NOT IN": "NOT IN",
        "EQUALS": "=",
        "NOT_EQUALS": "!=",
        "GREATER_THAN": ">",
        "LESS_THAN": "<",
        "GREATER_THAN_OR_EQUALS": ">=",
        "LESS_THAN_OR_EQUALS": "<=",
    }

    @property
    def uid(self) -> str:
        """Unique identifier for this datasource (id__type)."""
        return f"{self.id}__{self.type}"

    @property
    def column_names(self) -> list[str]:
        """Return a list of column names defined on this dataset."""
        return [c.column_name for c in (self.columns or [])]

    @property
    def datasource_type(self) -> str:
        """Return the datasource type identifier."""
        return self.type

    @property
    def datasource_name(self) -> str:
        """Return the underlying table name."""
        return self.table_name

    @property
    def name(self) -> str:
        """Fully-qualified name: schema.table_name or just table_name."""
        return f"{self.schema}.{self.table_name}" if self.schema else self.table_name

    @property
    def full_name(self) -> str:
        """Fully-qualified name including database and catalog.

        Format: ``[database].[catalog].[schema].[table_name]``, omitting
        empty parts, matching the original
        ``utils.get_datasource_full_name()`` behaviour.
        """
        parts: list[str] = []
        if not sa.inspect(self).unloaded.intersection({"database"}):
            db = self.database
            if db is not None:
                parts.append(f"[{db}]")
        if self.catalog:
            parts.append(f"[{self.catalog}]")
        if self.schema:
            parts.append(f"[{self.schema}]")
        parts.append(f"[{self.table_name}]")
        return ".".join(parts)

    @property
    def select_star(self) -> str | None:
        """Generate a ``SELECT * … LIMIT 100`` preview for this table.

        1:1 with the original ``SqlaTable.select_star`` (line 1331-1338):
        delegates to ``Database.select_star`` with ``show_cols`` /
        ``latest_partition`` False to skip the expensive DB inspection.
        """
        # Guard against an unloaded ``database`` relationship — a sync
        # lazy-load under asyncpg would raise MissingGreenlet.
        if sa.inspect(self).unloaded.intersection({"database"}):
            return None
        if self.database is None:
            return None

        # IMPORTANT: the module-level ``Table`` is ``sqlalchemy.Table`` (an ORM
        # construct whose 2nd positional arg is ``metadata``); the engine-spec
        # expects ``superset.sql.parse.Table`` (table / schema / catalog). Using
        # the wrong one raised ``AttributeError: 'str' object has no attribute
        # 'schema'`` — silently swallowed → ``select_star`` was always None.
        from superset.sql.parse import Table as ParsedTable

        table = ParsedTable(
            str(self.table_name),
            self.schema or None,
            self.catalog or None,
        )

        try:
            return self.database.select_star(
                table,
                show_cols=False,
                latest_partition=False,
            )
        except Exception:
            logger.warning(
                "Failed to generate select_star for table '%s'",
                self.table_name,
                exc_info=True,
            )
            return None

    def external_metadata(self) -> list[dict[str, Any]]:
        """Fetch column metadata from the underlying database.

        Matches original SqlaTable.external_metadata (line 1313-1321):
        - Virtual datasets (with custom SQL) -> get_virtual_table_metadata
        - Physical tables -> get_physical_table_metadata
        """

        if self.sql:
            return self._get_virtual_table_metadata()
        return self._get_physical_table_metadata()

    def _get_virtual_table_metadata(self) -> list[dict[str, Any]]:
        """Get column metadata for a virtual dataset (custom SQL query).

        Executes the SQL with a LIMIT 0 to get column names and types from the
        result set metadata.

        Execution errors are raised as ``SupersetGenericDBErrorException`` (1:1
        with the original ``get_virtual_table_metadata`` in
        ``superset_old/connectors/sqla/utils.py:99``) rather than swallowed to
        ``[]`` — otherwise a transient warehouse / invalid-SQL error during
        ``fetch_metadata`` would silently drop every introspected column via the
        ``all, delete-orphan`` cascade.
        """
        if not self.sql:
            return []

        from sqlalchemy import text as sa_text

        from superset.exceptions import (
            SupersetGenericDBErrorException,
            SupersetSyntaxErrorException,
        )
        from superset.utils.database import get_sync_connection

        # Process Jinja templates first (1:1 with the original
        # ``get_virtual_table_metadata``); a raw ``{{ }}`` would otherwise be
        # sent to the warehouse and fail. A template syntax error maps to a
        # ``SupersetGenericDBErrorException`` (400), as in the original.
        inner_sql = self.sql.strip().rstrip(";")
        try:
            inner_sql = self.get_template_processor().process_template(
                inner_sql, **self.template_params_dict
            )
        except SupersetSyntaxErrorException as ex:
            raise SupersetGenericDBErrorException(
                message=f"Template processing error: {ex}"
            ) from ex

        metadata_sql = f"SELECT * FROM ({inner_sql}) AS virtual_table LIMIT 0"  # noqa: S608

        try:
            with get_sync_connection(self.database) as (conn, spec):
                result = conn.execute(sa_text(metadata_sql))
                columns: list[dict[str, Any]] = []
                for col in result.cursor.description or []:
                    # ``cursor.description`` type codes are DBAPI-specific
                    # (psycopg2 returns int OIDs); map to a string type repr via
                    # the engine spec — 1:1 with the original ``SupersetResultSet``
                    # path (``get_datatype``).  Persisting the raw int code would
                    # crash on the VARCHAR ``TableColumn.type`` column.
                    type_repr = spec.get_datatype(col[1]) if len(col) > 1 else None
                    columns.append(
                        {"column_name": col[0], "type": type_repr}
                    )
                return columns
        except Exception as ex:
            raise SupersetGenericDBErrorException(message=f"Invalid SQL: {ex}") from ex

    def _get_physical_table_metadata(self) -> list[dict[str, Any]]:
        """Use a SQLAlchemy inspector to get physical-table column metadata.

        Async port of ``get_physical_table_metadata`` in
        ``superset_old/connectors/sqla/utils.py`` (line 50). The original
        sources columns via ``Database.get_columns``; liteset has no such model
        method, so columns are read directly from the engine spec + a sync
        inspector (``Database.get_inspector``). ``schema_options`` is forwarded
        so engine specs that expand nested columns (Trino ``expand_rows``)
        behave identically. Each column's SQLAlchemy ``type`` is converted to
        the dialect type string and enriched with ``type_generic`` / ``is_dttm``
        so the persisted ``TableColumn`` rows match the original contract.

        Introspection errors (e.g. a missing table) are *not* swallowed: the
        original raises ``NoSuchTableError`` so the dataset create / refresh API
        surfaces a proper error rather than silently producing zero columns.
        """
        from sqlalchemy.types import TypeEngine

        from superset.sql.parse import Table as ParsedTable

        db_engine_spec = self.db_engine_spec
        db_dialect = self.database.get_dialect()
        parsed_table = ParsedTable(
            str(self.table_name),
            self.schema or None,
            self.catalog or None,
        )

        with self.database.get_inspector(
            catalog=self.catalog or None,
            schema=self.schema or None,
        ) as inspector:
            cols = db_engine_spec.get_columns(
                inspector, parsed_table, self.database.schema_options
            )

        for col in cols:
            try:
                if isinstance(col["type"], TypeEngine):
                    name = col["column_name"]
                    if not self.normalize_columns:
                        name = db_engine_spec.denormalize_name(db_dialect, name)
                    db_type = db_engine_spec.column_datatype_to_string(
                        col["type"], db_dialect
                    )
                    type_spec = db_engine_spec.get_column_spec(
                        db_type, db_extra=self.database.get_extra()
                    )
                    col.update(
                        {
                            "name": name,
                            "column_name": name,
                            "type": db_type,
                            "type_generic": (
                                type_spec.generic_type if type_spec else None
                            ),
                            "is_dttm": type_spec.is_dttm if type_spec else None,
                        }
                    )
            # Broad catch: drivers raise a variety of errors outside CompileError.
            except Exception:  # noqa: BLE001
                col.update(
                    {
                        "type": "UNKNOWN",
                        "type_generic": None,
                        "is_dttm": None,
                    }
                )
        return cols

    def get_perm(self) -> str:
        """Return this dataset's permission name.

        Format: ``[database].[table_name](id:N)``
        """
        if sa.inspect(self).unloaded.intersection({"database"}):
            return self.perm or ""
        if self.database is None:
            raise ValueError("Cannot evaluate permission: database is None")
        return f"[{self.database}].[{self.table_name}](id:{self.id})"

    def get_schema_perm(self) -> str | None:
        """Return schema permission string: ``[database].[schema]``."""
        if sa.inspect(self).unloaded.intersection({"database"}):
            return self.schema_perm
        if self.database is None or not self.schema:
            return None
        db_name = getattr(self.database, "database_name", str(self.database))
        return f"[{db_name}].[{self.schema}]"

    def get_catalog_perm(self) -> str | None:
        """Return catalog permission string: ``[database].[catalog]``."""
        if sa.inspect(self).unloaded.intersection({"database"}):
            return self.catalog_perm
        if self.database is None or not self.catalog:
            return None
        db_name = getattr(self.database, "database_name", str(self.database))
        return f"[{db_name}].[{self.catalog}]"

    @property
    def columns_dict(self) -> dict[str, TableColumn]:
        """Map of column_name -> TableColumn."""
        return {c.column_name: c for c in (self.columns or [])}

    @property
    def metrics_dict(self) -> dict[str, SqlMetric]:
        """Map of metric_name -> SqlMetric."""
        return {m.metric_name: m for m in (self.metrics or [])}

    def add_missing_metrics(self, metrics: list[SqlMetric]) -> None:
        """Append metrics not already present on this dataset.

        1:1 with ``SqlaTable.add_missing_metrics`` in
        ``superset_old/connectors/sqla/models.py`` (line 310). Requires the
        ``metrics`` relationship to be loaded by the caller.
        """
        existing_metrics = {m.metric_name for m in self.metrics}
        for metric in metrics:
            if metric.metric_name not in existing_metrics:
                metric.table_id = self.id
                self.metrics.append(metric)

    @property
    def dttm_cols(self) -> list[str]:
        """Return column names marked as datetime.

        Matches original: includes main_dttm_col even if not marked is_dttm.
        """
        cols = [c.column_name for c in (self.columns or []) if c.is_dttm]
        if self.main_dttm_col and self.main_dttm_col not in cols:
            cols.append(self.main_dttm_col)
        return cols

    @property
    def any_dttm_col(self) -> str | None:
        """Return the first datetime column name, or None."""
        cols = self.dttm_cols
        return cols[0] if cols else None

    @property
    def num_cols(self) -> list[str]:
        """Return column names with numeric types."""
        return [c.column_name for c in (self.columns or []) if c.is_numeric]

    @property
    def column_formats(self) -> dict[str, str | None]:
        """Map of metric_name -> d3format for metrics that define one."""
        return {m.metric_name: m.d3format for m in (self.metrics or []) if m.d3format}

    @property
    def verbose_map(self) -> dict[str, str]:
        """Map identifiers to verbose names for display."""
        verb_map: dict[str, str] = {"__timestamp": "Time"}
        for m in self.metrics or []:
            if m.metric_name not in verb_map:
                verb_map[m.metric_name] = m.verbose_name or m.metric_name
        for c in self.columns or []:
            if c.column_name not in verb_map:
                verb_map[c.column_name] = c.verbose_name or c.column_name
        return verb_map

    @property
    def data(self) -> dict[str, Any]:
        """Full data representation of this datasource sent to the frontend."""
        db_data: dict[str, Any] = {}
        if not sa.inspect(self).unloaded.intersection({"database"}):
            db = self.database
            if db is not None and hasattr(db, "data"):
                db_data = db.data

        data_: dict[str, Any] = {
            # simple fields
            "id": self.id,
            "uid": self.uid,
            "column_formats": self.column_formats,
            "description": self.description,
            "database": db_data,
            "default_endpoint": self.default_endpoint,
            "filter_select": self.filter_select_enabled,
            "filter_select_enabled": self.filter_select_enabled,
            "name": self.name,
            "datasource_name": self.datasource_name,
            "table_name": self.datasource_name,
            "type": self.type,
            "catalog": self.catalog,
            "schema": self.schema or None,
            "offset": self.offset,
            "cache_timeout": self.cache_timeout,
            "params": self.params,
            "perm": self.perm,
            "edit_url": f"/tablemodelview/edit/{self.id}",
            # sqla-specific
            "sql": self.sql,
            # one to many
            "columns": [o.data for o in (self.columns or [])],
            "metrics": [o.data for o in (self.metrics or [])],
            "folders": self.folders,
            "order_by_choices": [],
            "owners": [owner.id for owner in (self.owners or [])],
            "verbose_map": self.verbose_map,
            "select_star": self.select_star,
        }

        # SqlaTable-specific extensions (matches original .data property)
        data_["granularity_sqla"] = [(c, c) for c in self.dttm_cols]
        data_["time_grain_sqla"] = [
            (g.duration, g.name) for g in self.database.grains() or []
        ]
        data_["main_dttm_col"] = self.main_dttm_col
        data_["fetch_values_predicate"] = self.fetch_values_predicate
        data_["template_params"] = self.template_params
        data_["is_sqllab_view"] = self.is_sqllab_view
        data_["health_check_message"] = None
        data_["extra"] = self.extra
        data_["owners"] = [
            {
                "first_name": getattr(o, "first_name", ""),
                "last_name": getattr(o, "last_name", ""),
                "username": getattr(o, "username", ""),
                "id": o.id,
            }
            for o in (self.owners or [])
        ]
        data_["always_filter_main_dttm"] = self.always_filter_main_dttm
        data_["normalize_columns"] = self.normalize_columns
        return data_

    @property
    def _backend(self) -> str:
        """Extract database backend name from the sqlalchemy_uri."""
        uri = getattr(self.database, "sqlalchemy_uri", "")
        if "://" in uri:
            return uri.split("://")[0].split("+")[0]
        return "postgresql"

    def _quote_identifier(self, name: str) -> str:
        """Quote an identifier using engine-appropriate quoting.

        MySQL uses backticks, MSSQL uses brackets, everything else
        uses standard double-quotes.
        """
        uri = getattr(self.database, "sqlalchemy_uri", "") or ""
        uri_lower = uri.lower()
        if "mysql" in uri_lower:
            return f"`{name}`"
        if "mssql" in uri_lower:
            return f"[{name}]"
        return f'"{name}"'

    def _quote_col_if_needed(self, name: str) -> str:
        """Quote a column reference only when the identifier actually
        requires quoting (mirrors SQLAlchemy's default behaviour for
        ``column()`` / ``literal_column()``).

        An identifier is returned unquoted when it consists solely of
        lowercase ASCII letters, digits, and underscores, does not start
        with a digit, and is not on the list of SQL reserved keywords.
        Everything else is passed through :meth:`_quote_identifier`.
        """
        if not name:
            return name
        if not (name[0].isalpha() or name[0] == "_"):
            return self._quote_identifier(name)
        for ch in name:
            if not (ch.isdigit() or ch == "_" or ("a" <= ch <= "z")):
                return self._quote_identifier(name)
        if name in _SQL_RESERVED_WORDS:
            return self._quote_identifier(name)
        return name

    def get_column(self, column_name: str | None) -> TableColumn | None:
        """Retrieve a TableColumn by name, or None."""
        if column_name is None:
            return None
        for col in self.columns or []:
            if col.column_name == column_name:
                return col
        return None

    def has_extra_cache_key_calls(self, query_obj: dict[str, Any]) -> bool:  # noqa: C901
        """Detect calls to ``ExtraCache`` template methods in templatable items.

        1:1 with ``SqlaTable.has_extra_cache_key_calls`` in
        ``superset_old/connectors/sqla/models.py`` (line 1859). If any present,
        the query must be evaluated to extract additional cache keys. This avoids
        executing the (potentially expensive) template code unnecessarily.

        :param query_obj: query object to analyze
        :return: True if there are call(s) to an ``ExtraCache`` method
        """
        from superset.jinja_context import ExtraCache
        from superset.utils.column import is_adhoc_column, is_adhoc_metric

        templatable_statements: list[str] = []
        if self.sql:
            templatable_statements.append(self.sql)
        if self.fetch_values_predicate:
            templatable_statements.append(self.fetch_values_predicate)
        extras = query_obj.get("extras", {})
        if "where" in extras:
            templatable_statements.append(extras["where"])
        if "having" in extras:
            templatable_statements.append(extras["having"])
        if columns := query_obj.get("columns"):
            calculated_columns: dict[str, Any] = {
                c.column_name: c.expression for c in self.columns if c.expression
            }
            for column_ in columns:
                if is_adhoc_column(column_):
                    templatable_statements.append(
                        column_.get("sqlExpression") or column_.get("sql_expression")
                    )
                elif isinstance(column_, str) and column_ in calculated_columns:
                    templatable_statements.append(calculated_columns[column_])
        if metrics := query_obj.get("metrics"):
            metrics_by_name: dict[str, Any] = {
                m.metric_name: m.expression for m in self.metrics
            }
            for metric in metrics:
                if is_adhoc_metric(metric) and (
                    sql := (
                        metric.get("sqlExpression") or metric.get("sql_expression")
                    )
                ):
                    templatable_statements.append(sql)
                elif isinstance(metric, str) and metric in metrics_by_name:
                    templatable_statements.append(metrics_by_name[metric])
        if self.is_rls_supported:
            templatable_statements += [
                clause for clause in self._get_rls_clause_strings()
            ]
        for statement in templatable_statements:
            if statement and ExtraCache.regex.search(statement):
                return True
        return False

    def _get_rls_clause_strings(self) -> list[str]:
        """Return the raw (untemplated) RLS clause strings for this table.

        Sync port of the ``f.clause for f in security_manager.get_rls_filters``
        iteration used by ``SqlaTable.has_extra_cache_key_calls`` /
        ``get_extra_cache_keys`` in the original. Resolves the current user from
        :func:`superset.utils.core.get_current_user` (set by auth middleware)
        and reads the matching filters via the sync RLS getter.
        """
        from superset.utils.core import get_current_user
        from superset.utils.rls import (
            _sync_get_rls_filters_for_user,
            _sync_resolve_user_role_ids,
        )

        user = get_current_user()
        user_role_ids = _sync_resolve_user_role_ids(user)
        if user_role_ids is None:
            return []
        return [
            f.clause for f in _sync_get_rls_filters_for_user(self.id, user_role_ids)
        ]

    def get_extra_cache_keys(self, query_obj: dict[str, Any]) -> list[Hashable]:
        """
        The cache key of a SqlaTable needs to consider any keys added by the
        parent class and any keys added via ``ExtraCache``.

        For virtual datasets, RLS predicates are included in the cache key to
        ensure users with different RLS rules get different cached results.

        1:1 with ``SqlaTable.get_extra_cache_keys`` in
        ``superset_old/connectors/sqla/models.py`` (line 1909).

        :param query_obj: query object to analyze
        :return: The extra cache keys
        """
        from superset.utils.rls import collect_rls_predicates_for_sql

        extra_cache_keys = super().get_extra_cache_keys(query_obj)
        if self.has_extra_cache_key_calls(query_obj):
            sqla_query = self.get_sqla_query(**query_obj)
            extra_cache_keys += sqla_query.extra_cache_keys

        # For virtual datasets, include RLS predicates in the cache key
        if self.is_virtual and self.sql:
            default_schema = self.database.get_default_schema(self.catalog)
            rls_predicates = collect_rls_predicates_for_sql(
                self.sql,
                self.database,
                self.catalog,
                self.schema or default_schema or "",
            )
            # Add each predicate as a separate cache key component
            extra_cache_keys.extend(rls_predicates)

        return list(set(extra_cache_keys))

    # -- AST hooks used by helpers.ExploreMixin.get_sqla_query --------------

    @property
    def db_extra(self) -> dict[str, Any] | None:
        """Return the database extra JSON dict."""
        try:
            if self.database is not None and hasattr(self.database, "get_extra"):
                return self.database.get_extra()
        except Exception:  # noqa: BLE001
            return None
        return None

    @property
    def db_engine_spec(self) -> Any:
        """Return the database engine spec (heavy version)."""
        return self.database.db_engine_spec if self.database is not None else None

    @property
    def is_virtual(self) -> bool:
        """True if this dataset wraps a custom SQL query (vs. a physical table).

        1:1 with ``SqlaTable.is_virtual`` in
        ``superset_old/connectors/sqla/models.py``.
        """
        return bool(self.sql)

    def get_sqla_table(self) -> Any:
        """Return a SQLAlchemy ``TableClause`` for the physical table.

        1:1 with ``SqlaTable.get_sqla_table`` in
        ``superset_old/connectors/sqla/models.py``
        (line 1400). Honours BigQuery-style cross-catalog queries by
        baking the catalog into a manually-quoted identifier.
        """
        from sqlalchemy.sql import quoted_name as _quoted_name, table as _sa_table

        spec = self.db_engine_spec
        supports_cross_catalog = bool(
            getattr(spec, "supports_cross_catalog_queries", False)
        )
        if self.catalog and supports_cross_catalog:
            # SQLAlchemy doesn't have built-in catalog support for
            # ``TableClause``; manually construct the identifier with
            # proper dialect-specific quoting.
            quote_id = self.database.quote_identifier
            catalog_q = quote_id(self.catalog)
            table_q = quote_id(self.table_name)
            if self.schema:
                schema_q = quote_id(self.schema)
                full = f"{catalog_q}.{schema_q}.{table_q}"
            else:
                full = f"{catalog_q}.{table_q}"
            return _sa_table(_quoted_name(full, quote=False))

        if self.schema:
            return _sa_table(self.table_name, schema=self.schema)
        return _sa_table(self.table_name)

    def get_from_clause(
        self, template_processor: Any | None = None
    ) -> tuple[Any, str | None]:
        """Return ``(FromClause, optional_cte)`` for this dataset.

        1:1 with ``SqlaTable.get_from_clause`` in
        ``superset_old/connectors/sqla/models.py``
        (line 1425). Physical datasets short-circuit to
        :meth:`get_sqla_table`; virtual datasets fall through to
        :meth:`ExploreMixin.get_from_clause` which renders the user
        SQL, applies RLS predicates inside the subquery, and decides
        whether the engine needs a CTE hoisted to the top.
        """
        if not self.is_virtual:
            return self.get_sqla_table(), None
        from superset.models.helpers import ExploreMixin

        return ExploreMixin.get_from_clause(self, template_processor)

    @property
    def template_params_dict(self) -> dict[str, Any]:
        """Return parsed ``template_params`` JSON, or empty dict.

        1:1 with ``SqlaTable.template_params_dict`` in
        ``superset_old/connectors/sqla/models.py``.
        """
        import json as _json

        try:
            if self.template_params:
                value = _json.loads(self.template_params)
                if isinstance(value, dict):
                    return value
        except (ValueError, TypeError):
            pass
        return {}

    def get_template_processor(self, **kwargs: Any) -> Any:
        """Return a Jinja template processor for this dataset.

        1:1 with ``SqlaTable.get_template_processor`` in
        ``superset_old/connectors/sqla/models.py``
        (line 1397).
        """
        from superset.jinja_context import get_template_processor

        return get_template_processor(table=self, database=self.database, **kwargs)

    def make_sqla_column_compatible(
        self, sqla_col: Any, label: str | None = None
    ) -> Any:
        """Apply engine-spec label compatibility (Oracle truncation, etc.).

        Delegates to :meth:`Database.make_sqla_column_compatible` which
        is the 1:1 port of the original. The wrapper exists because
        :class:`ExploreMixin` calls
        ``self.make_sqla_column_compatible`` directly.
        """
        return self.database.make_sqla_column_compatible(sqla_col, label)

    def convert_tbl_column_to_sqla_col(
        self,
        tbl_column: TableColumn,
        label: str | None = None,
        template_processor: Any | None = None,
    ) -> Any:
        """Wrap a ``TableColumn`` as a SQLAlchemy expression.

        1:1 with ``SqlaTable.convert_tbl_column_to_sqla_col`` in
        ``superset_old/connectors/sqla/models.py``
        (line ~860). Delegates to :meth:`TableColumn.get_sqla_col`
        which produces a properly-typed and properly-labelled
        ``ColumnElement`` honouring the engine spec.
        """
        return tbl_column.get_sqla_col(
            label=label, template_processor=template_processor
        )

    def get_fetch_values_predicate(
        self,
        template_processor: Any | None = None,
    ) -> Any:
        """Return a SQLAlchemy ``TextClause`` for the fetch-values predicate.

        1:1 with ``SqlaTable.get_fetch_values_predicate`` in
        ``superset_old/connectors/sqla/models.py``
        (line 1377). Used by ``apply_fetch_values_predicate`` and
        :meth:`values_for_column` to scope filter-dropdown queries.
        """
        from jinja2.exceptions import TemplateError

        from superset.exceptions import (
            QueryObjectValidationError,
            SupersetSyntaxErrorException,
        )

        fetch_values_predicate = self.fetch_values_predicate
        if not fetch_values_predicate:
            return None

        if template_processor:
            try:
                fetch_values_predicate = template_processor.process_template(
                    fetch_values_predicate
                )
            except (TemplateError, SupersetSyntaxErrorException) as ex:
                raise QueryObjectValidationError(
                    f"Error in jinja expression in fetch values predicate: {ex}"
                ) from ex

        try:
            return self.db_engine_spec.get_text_clause(fetch_values_predicate)
        except Exception as ex:  # noqa: BLE001
            raise QueryObjectValidationError(
                f"Error in fetch values predicate: {ex}"
            ) from ex

    def adhoc_metric_to_sqla(
        self,
        metric: dict[str, Any],
        columns_by_name: dict[str, TableColumn],
        template_processor: Any | None = None,
        processed: bool = False,
    ) -> Any:
        """Convert an adhoc metric dict to a SQLAlchemy ``ColumnElement``.

        1:1 with ``SqlaTable.adhoc_metric_to_sqla`` in
        ``superset_old/connectors/sqla/models.py``
        (line 1434). Resolves SIMPLE adhoc metrics through
        ``TableColumn.get_sqla_col`` so calculated columns retain
        their expression — the bug-fix the original made over the
        helpers-mixin version.
        """
        from typing import cast as _cast

        from sqlalchemy import column as sa_column
        from sqlalchemy.sql import literal_column

        from superset.exceptions import (
            QueryObjectValidationError,
            SupersetSecurityException,
        )
        from superset.models.helpers import AdhocMetricExpressionType
        from superset.utils.column import get_metric_name

        # Support both camelCase (frontend payload, original Apache Superset
        # convention) and snake_case (msgspec ``rename="camel"`` round-trips
        # may surface either form).
        expression_type = metric.get("expressionType") or metric.get(
            "expression_type"
        )
        # 1:1 with original — passes the dataset's ``verbose_map`` so
        # SIMPLE adhoc metrics that reference a TableColumn render
        # with the user-friendly verbose name when one is configured.
        label = get_metric_name(metric, self.verbose_map)

        if expression_type == AdhocMetricExpressionType.SIMPLE:
            metric_column = metric.get("column") or {}
            column_name = _cast(
                str,
                metric_column.get("column_name") or metric_column.get("columnName"),
            )
            table_column: TableColumn | None = columns_by_name.get(column_name)
            if table_column:
                sqla_column = table_column.get_sqla_col(
                    template_processor=template_processor
                )
            else:
                sqla_column = sa_column(column_name)
            sqla_metric = self.sqla_aggregations[metric["aggregate"]](sqla_column)
        elif expression_type == AdhocMetricExpressionType.SQL:
            expression = metric.get("sqlExpression") or metric.get("sql_expression")
            if not processed:
                try:
                    expression = self._process_select_expression(
                        expression=expression,
                        database_id=self.database_id,
                        engine=self.database.backend,
                        schema=self.schema,
                        template_processor=template_processor,
                    )
                except SupersetSecurityException as ex:
                    # 1:1 with original — surface the structured
                    # ``ex.message`` (a SupersetError summary) rather
                    # than ``str(ex)`` which would include the full
                    # ``SupersetSecurityException`` repr and leak
                    # internal trace information into chart-data
                    # validation errors.
                    raise QueryObjectValidationError(ex.message) from ex
            sqla_metric = literal_column(expression)
        else:
            raise QueryObjectValidationError("Adhoc metric expressionType is invalid")

        return self.make_sqla_column_compatible(sqla_metric, label)

    def adhoc_column_to_sqla(
        self,
        col: dict[str, Any],
        force_type_check: bool = False,
        template_processor: Any | None = None,
    ) -> Any:
        """Turn an adhoc column dict into a SQLAlchemy ``ColumnElement``.

        1:1 with ``SqlaTable.adhoc_column_to_sqla`` in
        ``superset_old/connectors/sqla/models.py``
        (line 1486). Honours:

        - ``isColumnReference``: quote bare identifiers via the dialect
          preparer so reserved words / mixed-case names round-trip
          correctly.
        - Time-grain on adhoc BASE_AXIS columns: dispatch to
          ``db_engine_spec.get_timestamp_expr`` after probing the
          column type when ``force_type_check`` is set.
        - Calculated columns referenced by name in ``sqlExpression``:
          fall through to ``TableColumn.get_sqla_col`` which carries
          the calculated-column expression.
        """
        import sqlalchemy as _sa
        from sqlalchemy.sql import literal_column

        from superset.exceptions import (
            ColumnNotFoundException,
            QueryObjectValidationError,
            SupersetSecurityException,
        )
        from superset.utils.column import get_column_name

        label = get_column_name(col)
        # Support both camelCase (frontend payload) and snake_case
        # (msgspec ``rename="camel"`` round-trips) — see
        # ``adhoc_metric_to_sqla`` for the same pattern.
        sql_expression = col.get("sqlExpression") or col.get("sql_expression")
        if sql_expression is None:
            sql_expression = col["sqlExpression"]  # raise the original KeyError
        time_grain = col.get("timeGrain") or col.get("time_grain")
        column_type = col.get("columnType") or col.get("column_type")
        has_timegrain = column_type == "BASE_AXIS" and time_grain
        is_dttm = False
        pdf: str | None = None
        is_column_reference = col.get("isColumnReference") or col.get(
            "is_column_reference", False
        )

        # First check if this references a known TableColumn — happens
        # when the user picks a calculated column from the adhoc
        # picker.  In that case we use the column's own
        # ``get_sqla_col`` so the expression survives.
        col_in_metadata = self.get_column(sql_expression)
        if col_in_metadata is not None:
            sqla_column = col_in_metadata.get_sqla_col(
                template_processor=template_processor
            )
            is_dttm = col_in_metadata.is_temporal
            pdf = col_in_metadata.python_date_format
        else:
            try:
                expression_to_process = sql_expression
                if is_column_reference:
                    expression_to_process = self.database.quote_identifier(
                        sql_expression
                    )
                expression = self._process_select_expression(
                    expression=expression_to_process,
                    database_id=self.database_id,
                    engine=self.database.backend,
                    schema=self.schema,
                    template_processor=template_processor,
                )
            except SupersetSecurityException as ex:
                raise QueryObjectValidationError(str(ex)) from ex

            sqla_column = literal_column(expression)
            if has_timegrain or force_type_check:
                # Probe the adhoc column's type by running a LIMIT 1
                # SELECT through the database. This matches the
                # original behaviour where ``get_columns_description``
                # (which relies on the sync engine) is used to decide
                # whether the adhoc column is temporal so it can be
                # bucketed via ``get_timestamp_expr``.
                try:
                    tbl, _cte = self.get_from_clause(template_processor)
                    qry = _sa.select(sqla_column).limit(1).select_from(tbl)
                    sql = self.database.compile_sqla_query(
                        qry,
                        catalog=self.catalog,
                        schema=self.schema,
                    )
                    is_dttm = self._probe_adhoc_column_is_dttm(sql)
                except QueryObjectValidationError:
                    raise
                except Exception as ex:  # noqa: BLE001
                    raise ColumnNotFoundException(
                        message=f"Could not probe adhoc column type: {ex}"
                    ) from ex

        if is_dttm and has_timegrain:
            sqla_column = self.db_engine_spec.get_timestamp_expr(
                col=sqla_column,
                pdf=pdf,
                time_grain=time_grain,
            )

        return self.make_sqla_column_compatible(sqla_column, label)

    def _probe_adhoc_column_is_dttm(self, sql: str) -> bool:
        """Return True if executing ``sql`` yields a temporal column.

        Helper for :meth:`adhoc_column_to_sqla` — runs a LIMIT 1
        introspection query through the sync engine and asks the
        database's engine spec to interpret the DBAPI ``cursor.description``
        type codes via :meth:`BaseEngineSpec.get_column_spec`.

        1:1 with ``get_columns_description`` in
        ``superset_old/connectors/sqla/utils.py``
        and ``superset_old/result_set.py:SupersetResultSet.is_temporal``:
        the original wraps the query in
        :class:`SupersetResultSet` which derives ``is_dttm`` from
        ``db_engine_spec.get_column_spec(native_type).is_dttm`` — *not*
        from a brittle string-match on the DBAPI type-code repr.
        """
        try:
            from sqlalchemy import text as sa_text

            from superset.utils.database import get_sync_connection

            with get_sync_connection(self.database) as (conn, _spec):
                result = conn.execute(sa_text(sql))
                description = getattr(result.cursor, "description", None) or []
                if not description:
                    return False

                # The probe SELECT projects only the adhoc column, so
                # ``description[0]`` is the relevant entry.  ``type_code``
                # may be a class, an int constant or a string depending
                # on the driver; the engine spec's ``get_column_spec``
                # accepts a native-type *string*, so coerce.
                desc_row = description[0]
                if len(desc_row) < 2:
                    return False
                type_code: Any = desc_row[1]

                # Coerce the DBAPI type-code into a string the engine
                # spec can map.  This mirrors
                # ``superset_old/result_set.py:convert_to_string`` —
                # str | bytes pass through, anything else is stringified.
                if isinstance(type_code, bytes):
                    native_type = type_code.decode("utf-8")
                elif isinstance(type_code, str):
                    native_type = type_code
                else:
                    # SQLAlchemy stores the class on its way out (e.g.
                    # ``<class 'cx_Oracle.DATETIME'>``).  Use ``__name__``
                    # when present so the engine-spec regex tables hit;
                    # otherwise fall back to ``str(...)``.
                    native_type = getattr(type_code, "__name__", None) or str(type_code)

                spec = self.db_engine_spec
                if spec is None:
                    return False
                try:
                    column_spec = spec.get_column_spec(
                        native_type,
                        db_extra=self.database.get_extra(),
                    )
                except Exception:  # noqa: BLE001
                    return False
                return bool(column_spec and column_spec.is_dttm)
        except Exception:  # noqa: BLE001
            return False

    def clone(self) -> SqlaTable:
        """Create a copy of this dataset.

        Ported from superset_old/commands/dataset/duplicate.py.
        Copies key fields and deep-copies columns and metrics into new
        instances (without IDs) so the clone can be persisted independently.
        """
        table = SqlaTable(
            table_name=self.table_name,
            database_id=self.database_id,
            schema=self.schema,
            catalog=self.catalog,
            sql=self.sql,
            is_sqllab_view=self.is_sqllab_view,
            template_params=self.template_params,
            normalize_columns=self.normalize_columns,
            always_filter_main_dttm=self.always_filter_main_dttm,
            main_dttm_col=self.main_dttm_col,
            fetch_values_predicate=self.fetch_values_predicate,
            extra=self.extra,
            description=self.description,
            default_endpoint=self.default_endpoint,
            offset=self.offset,
            cache_timeout=self.cache_timeout,
            params=self.params,
            filter_select_enabled=self.filter_select_enabled,
            folders=self.folders,
        )

        # Deep-copy columns
        cols: list[TableColumn] = []
        for c in self.columns or []:
            cols.append(
                TableColumn(
                    column_name=c.column_name,
                    verbose_name=c.verbose_name,
                    expression=c.expression,
                    filterable=c.filterable,
                    groupby=c.groupby,
                    is_dttm=c.is_dttm,
                    type=c.type,
                    description=c.description,
                    is_active=c.is_active,
                    advanced_data_type=c.advanced_data_type,
                    python_date_format=c.python_date_format,
                    extra=c.extra,
                )
            )
        table.columns = cols

        # Deep-copy metrics
        mets: list[SqlMetric] = []
        for m in self.metrics or []:
            mets.append(
                SqlMetric(
                    metric_name=m.metric_name,
                    verbose_name=m.verbose_name,
                    expression=m.expression,
                    metric_type=m.metric_type,
                    description=m.description,
                    d3format=m.d3format,
                    currency=m.currency,
                    warning_text=m.warning_text,
                    extra=m.extra,
                )
            )
        table.metrics = mets

        return table

    def to_dict(self) -> dict[str, Any]:
        """Serialize this dataset to a plain dictionary.

        Returns a dict of the key model fields suitable for JSON
        serialization or comparison.  Does NOT include relationships
        (columns, metrics, owners) to avoid lazy-load side-effects;
        use the ``data`` property for the full frontend payload.
        """
        return {
            "id": self.id,
            "table_name": self.table_name,
            "database_id": self.database_id,
            "schema": self.schema,
            "catalog": self.catalog,
            "sql": self.sql,
            "is_sqllab_view": self.is_sqllab_view,
            "template_params": self.template_params,
            "main_dttm_col": self.main_dttm_col,
            "fetch_values_predicate": self.fetch_values_predicate,
            "description": self.description,
            "default_endpoint": self.default_endpoint,
            "offset": self.offset,
            "cache_timeout": self.cache_timeout,
            "params": self.params,
            "perm": self.perm,
            "schema_perm": self.schema_perm,
            "catalog_perm": self.catalog_perm,
            "filter_select_enabled": self.filter_select_enabled,
            "extra": self.extra,
            "normalize_columns": self.normalize_columns,
            "always_filter_main_dttm": self.always_filter_main_dttm,
            "folders": self.folders,
            "is_managed_externally": self.is_managed_externally,
            "external_url": self.external_url,
        }

    # -- SQL generation -------------------------------------------------------

    def _get_table_ref(self) -> str:
        """Return fully-qualified table reference for FROM clause.

        For virtual datasets this wraps the user-provided SQL as a
        subquery: ``(user_sql) AS virtual_table``.  Callers that need
        to support databases where CTEs cannot appear inside a
        subquery (e.g., MSSQL, Ocient) should use
        :meth:`_get_virtual_from_clause` instead, which returns the
        CTE text separately so it can be prepended to the full SQL.
        """
        if self.sql:
            # Virtual dataset — wrap the custom SQL as a subquery
            inner = self.sql.strip().rstrip(";")
            return f"({inner}) AS virtual_table"
        parts: list[str] = []
        if self.catalog:
            parts.append(self._quote_identifier(str(self.catalog)))
        if self.schema:
            parts.append(self._quote_identifier(str(self.schema)))
        parts.append(self._quote_identifier(str(self.table_name)))
        return ".".join(parts)

    def _get_virtual_from_clause(self) -> tuple[str, str | None]:
        """Return ``(table_ref, cte_sql)`` for use in a SELECT.

        Async port of the original ``helpers.get_from_clause`` +
        ``_apply_cte`` pair (``superset_old/models/helpers.py:1163``
        and line 942).  Produces the same output shape:

        - For a physical dataset → ``(fully.qualified.table, None)``.
        - For a virtual dataset on an engine that supports CTEs inside
          subqueries (the default) → ``((user_sql) AS virtual_table,
          None)`` — same as :meth:`_get_table_ref`.
        - For a virtual dataset on an engine that does *not* allow
          CTEs inside a subquery (e.g., MSSQL, Ocient) *and* whose
          user SQL itself starts with ``WITH`` → returns
          ``(__cte, 'WITH __cte AS (...)')`` so callers can prepend
          the CTE text to the final SQL and select from the alias.
        """
        if not self.sql:
            return self._get_table_ref(), None

        inner = self.sql.strip().rstrip(";")
        engine_spec = self.database.db_engine_spec
        cte: str | None = None
        try:
            cte = engine_spec.get_cte_query(inner)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to detect CTE in virtual dataset SQL for table %s",
                self.table_name,
                exc_info=True,
            )
            cte = None

        if cte:
            # The engine spec's ``get_cte_query`` has rewritten the
            # SELECT statement to be aliased via ``cte_alias``.  The
            # FROM clause then selects from that alias.
            return engine_spec.cte_alias, cte

        # Default virtual-dataset handling: wrap as subquery alias.
        return f"({inner}) AS virtual_table", None

    @staticmethod
    def _apply_cte(sql: str, cte: str | None) -> str:
        """Prepend a CTE statement to a SELECT statement.

        Mirrors ``superset_old.models.helpers._apply_cte`` (line 942):
        when the engine spec determined the user's virtual-dataset SQL
        must be hoisted into a top-level CTE, the rendered SELECT is
        concatenated after the CTE text.
        """
        if cte:
            return f"{cte}\n{sql}"
        return sql

    def _resolve_metric_expression(self, metric: Any) -> tuple[str, str]:
        """Resolve a metric to (sql_expression, label).

        Handles three forms:
        1. String metric name — look up in SqlMetric definitions
        2. Adhoc SIMPLE metric — {expressionType: "SIMPLE", column: {...}}
        3. Adhoc SQL metric — {expressionType: "SQL", sqlExpression: "..."}

        Returns (expression_sql, label).
        """
        if isinstance(metric, str):
            # Named metric — look up in the dataset's metric definitions
            metrics_by_name = {m.metric_name: m for m in (self.metrics or [])}
            if metric in metrics_by_name:
                m = metrics_by_name[metric]
                return m.expression, metric
            raise ValueError(f"Metric not found: {metric}")

        if isinstance(metric, dict):
            label = (
                metric.get("label")
                or metric.get("optionName")
                or metric.get("option_name")
                or str(metric)
            )
            expr_type = (
                metric.get("expressionType") or metric.get("expression_type") or ""
            )

            if expr_type == "SIMPLE":
                col_obj = metric.get("column") or {}
                col_name = col_obj.get("column_name", "*")
                # If the column has an expression (calculated/virtual column),
                # use it instead of the physical column name.
                # Matches original adhoc_metric_to_sqla + make_sqla_column logic.
                col_expression = col_obj.get("expression")
                aggregate = metric.get("aggregate", "COUNT")
                tmpl = self._SQLA_AGGREGATIONS.get(
                    aggregate, f"{aggregate.lower()}({{col}})"
                )
                if col_expression:
                    col_ref = col_expression
                elif col_name != "*":
                    # Use SQLAlchemy-style conditional quoting so simple
                    # lowercase identifiers like ``num_girls`` remain
                    # unquoted; matches original ``sa.func.SUM(column(...))``
                    # compilation output.
                    col_ref = self._quote_col_if_needed(col_name)
                else:
                    col_ref = col_name
                return tmpl.format(col=col_ref), label

            if expr_type == "SQL":
                sql_expr = (
                    metric.get("sqlExpression")
                    or metric.get("sql_expression")
                    or "COUNT(*)"
                )
                return sql_expr, label

        # Fallback
        return "COUNT(*)", str(metric)

    def _resolve_column_expression(self, col: Any) -> tuple[str, str]:
        """Resolve a column spec to (sql_expression, label).

        Handles:
        1. String column name
        2. Adhoc column dict with sqlExpression and label
        """
        if isinstance(col, str):
            # Check if column has a custom expression
            col_obj = self.get_column(col)
            if col_obj and col_obj.expression:
                return col_obj.expression, col
            return self._quote_identifier(col), col

        if isinstance(col, dict):
            sql_expr = col.get("sqlExpression") or col.get("sql_expression")
            label = col.get("label") or sql_expr or ""
            if sql_expr:
                return sql_expr, label
            col_name = col.get("column_name") or label or ""
            return self._quote_identifier(str(col_name)), label

        return str(col), str(col)

    def _get_time_grain_expr(self, col_name: str, time_grain: str | None) -> str:
        """Apply time grain truncation to a column using engine-specific expressions."""
        from superset.utils.database import get_engine_spec_for_database

        spec = get_engine_spec_for_database(self.database)
        grain_exprs = spec.get_time_grain_expressions()
        col_ref = self._quote_identifier(col_name)

        if time_grain and time_grain in grain_exprs:
            return grain_exprs[time_grain].replace("{col}", col_ref)
        if None in grain_exprs:
            return grain_exprs[None].replace("{col}", col_ref)
        return col_ref

    def _build_filter_clause(  # noqa: C901
        self,
        flt: dict[str, Any],
        from_dttm: datetime | str | None,
        to_dttm: datetime | str | None,
    ) -> str | None:
        """Convert a single filter dict to a SQL WHERE fragment."""
        col = flt.get("col")
        op = flt.get("op", "")
        val = flt.get("val")

        if not col or not op:
            return None

        op_upper = op.upper().strip()

        # Mirror the original ``BaseDatasource._get_sqla_query`` behaviour:
        # if the filter references a column that doesn't exist on this
        # datasource (and isn't an adhoc/SQL expression), silently drop
        # the filter rather than generating SQL that the database will
        # reject. The column is recorded under ``rejected_filter_columns``
        # in the response payload by ``query_context_processor``. Only
        # plain column-name strings are validated; dicts (adhoc columns)
        # are passed through unchanged.
        #
        # This check runs BEFORE TEMPORAL_RANGE handling because a
        # TEMPORAL_RANGE filter referencing a non-existent datetime
        # column would still generate SQL like ``"ds" >= ...`` that
        # fails at execute. Cypress tests override datasources (e.g.
        # graph test targets energy_usage which has no ``ds`` column)
        # and rely on stale temporal filters being silently dropped.
        if isinstance(col, str):
            known_cols = {c.column_name for c in (self.columns or [])}
            if known_cols and col not in known_cols:
                return None

        # Handle TEMPORAL_RANGE filter
        if op_upper == "TEMPORAL_RANGE" and isinstance(val, str):
            return self._build_temporal_range_filter(col, val)

        qcol = self._quote_identifier(col)

        # Handle IS NULL / IS NOT NULL
        if op_upper in ("IS NULL", "IS_NULL"):
            return f"{qcol} IS NULL"
        if op_upper in ("IS NOT NULL", "IS_NOT_NULL"):
            return f"{qcol} IS NOT NULL"

        # Handle IS TRUE / IS FALSE
        if op_upper in ("IS TRUE", "IS_TRUE"):
            return f"{qcol} IS TRUE"
        if op_upper in ("IS FALSE", "IS_FALSE"):
            return f"{qcol} IS FALSE"

        # Handle IN / NOT IN
        if op_upper in ("IN", "NOT IN", "NOT_IN"):
            values = val if isinstance(val, list) else [val]
            if not values:
                return None
            escaped = []
            for v in values:
                if v is None:
                    escaped.append("NULL")
                elif isinstance(v, (int, float)):
                    escaped.append(str(v))
                else:
                    escaped.append(f"'{_escape_sql_string(str(v))}'")
            in_list = ", ".join(escaped)
            negation = "NOT " if "NOT" in op_upper else ""
            return f"{qcol} {negation}IN ({in_list})"

        # Handle LIKE / ILIKE / NOT LIKE
        if op_upper in ("LIKE", "ILIKE", "NOT_LIKE", "NOT LIKE"):
            escaped_val = _escape_sql_string(str(val)) if val is not None else ""
            sql_op = op_upper.replace("_", " ")
            return f"{qcol} {sql_op} '{escaped_val}'"

        # Handle comparison operators
        sql_op = self._FILTER_OPS.get(op_upper, op_upper)
        if val is None:
            if sql_op in ("=", "EQUALS"):
                return f"{qcol} IS NULL"
            if sql_op in ("!=", "NOT_EQUALS"):
                return f"{qcol} IS NOT NULL"
            return None

        if isinstance(val, (int, float)):
            return f"{qcol} {sql_op} {val}"
        return f"{qcol} {sql_op} '{_escape_sql_string(str(val))}'"

    def _build_temporal_range_filter(self, col: str, time_range: str) -> str | None:
        """Build a WHERE clause from a TEMPORAL_RANGE filter value.

        Uses the full get_since_until() parser which handles ISO dates,
        relative expressions like "7 days ago", and complex expressions
        like "DATETRUNC(DATETIME('today'), WEEK)".
        """
        from superset.utils.date import get_since_until

        if not time_range or time_range.lower() == "no filter":
            return None

        try:
            since_dt, until_dt = get_since_until(time_range=time_range)
        except (ValueError, Exception):
            logger.warning(
                "Failed to parse temporal range '%s' for col '%s'",
                time_range,
                col,
                exc_info=True,
            )
            return None

        qcol = self._quote_identifier(col)
        clauses: list[str] = []
        if since_dt:
            clauses.append(f"{qcol} >= '{since_dt.isoformat()}'")
        if until_dt:
            clauses.append(f"{qcol} < '{until_dt.isoformat()}'")

        return " AND ".join(clauses) if clauses else None

    # ------------------------------------------------------------------
    # SQL build pipeline — Strategy A: wire-up
    # ``helpers.ExploreMixin.get_sqla_query``
    # ------------------------------------------------------------------

    # -- Async query execution ------------------------------------------------

    async def async_values_for_column(  # noqa: C901  # complex business logic
        self,
        column_name: str,
        limit: int = 10000,
        rls_filters: list[Any] | None = None,
        denormalize_column: bool = False,
    ) -> list[Any]:
        """Return distinct values of ``column_name`` for filter dropdowns.

        Async port of
        ``superset_old.models.helpers.values_for_column``.
        Builds ``SELECT DISTINCT <col> AS column_values FROM <table>
        [WHERE <fetch_values_predicate> AND <rls_filters>] LIMIT <n>`` and
        executes it via the dataset's async engine.

        Args:
            column_name: The dataset column whose distinct values are
                requested.
            limit: Maximum number of distinct values to return.
            rls_filters: Optional list of Row-Level Security clauses.
                Preferred form: ``list[ClauseElement]`` (``TextClause`` /
                ``BooleanClauseList``) returned by
                :func:`superset.utils.rls.compose_rls_where_clauses` —
                each clause is compiled against the database's SQL
                dialect via ``self.database.get_dialect()`` so identifier
                quoting and dialect-specific translation happen
                automatically.  Backward-compat: raw SQL ``str``
                fragments are still accepted.  The caller (controller)
                is responsible for obtaining these from the security
                manager via ``get_rls_filters`` — mirrors the pattern
                used by ``async_query`` and matches the original sync
                ``values_for_column`` which calls
                ``self.get_sqla_row_level_filters`` and ANDs them into
                the WHERE clause.
        """
        from sqlalchemy import and_
        from sqlalchemy.sql import text as sa_text
        from sqlalchemy.sql.elements import ClauseElement

        # Denormalize the column name before querying for values unless disabled
        # in the dataset configuration — 1:1 with upstream ``values_for_column``.
        # ``denormalize_name`` is a no-op except on dialects that normalize
        # identifiers (Oracle/Snowflake), so the common case is unchanged.
        if denormalize_column:
            column_name = self.database.db_engine_spec.denormalize_name(
                self.database.get_dialect(), column_name
            )

        cols = {c.column_name: c for c in (self.columns or [])}
        if column_name not in cols:
            raise KeyError(column_name)

        target_col = cols[column_name]

        # Build a single template processor up-front and reuse it for both the
        # SELECT column and the fetch-values predicate — 1:1 with original
        # ``helpers.values_for_column`` (line 1570) which creates ``tp`` once
        # via ``self.get_template_processor()`` and threads it through
        # ``get_sqla_col`` / ``get_fetch_values_predicate``.
        from superset.jinja_context import get_template_processor

        processor = get_template_processor(database=self.database, table=self)

        # Use ``TableColumn.get_sqla_col`` so calculated columns get their
        # ``expression`` run through Jinja templating (e.g. macros referencing
        # ``{{ current_user_id() }}``) exactly like the original which uses
        # ``target_col.get_sqla_col(template_processor=tp).label(...)``.
        # ``process_template`` is pure CPU/Jinja work with no I/O, so it is
        # safe to run inside this coroutine; hand it to a worker thread to
        # match how the other sync helpers are invoked from the async path.
        select_col = (
            await asyncio.to_thread(
                target_col.get_sqla_col,
                None,
                processor,
            )
        ).label("column_values")

        # Resolve the FROM clause as a native SQLAlchemy AST node.
        # For virtual datasets this may produce a CTE that has to be
        # prepended to the final SQL — matches original
        # ``helpers.values_for_column`` (lines 1569 and 1593) where
        # ``get_from_clause`` returns ``(tbl, cte)`` and ``_apply_cte``
        # hoists the CTE above the SELECT.
        from_clause, cte_sql = self._build_from_ast()

        # Build the AST: SELECT DISTINCT <col> AS column_values FROM <tbl>
        qry = sa.select(select_col).distinct().select_from(from_clause)

        # Assemble WHERE clause from fetch_values_predicate + RLS filters.
        # Matches original ``helpers.values_for_column`` (lines 1585-1589)
        # where both predicates are ANDed together — but here we add each
        # as a SQLAlchemy ``ClauseElement`` and let ``and_(...)`` compose
        # them so RLS clauses (which may be ``BooleanClauseList`` /
        # ``or_(...)`` for group_key OR-within / AND-across) integrate as
        # native AST nodes rather than via string concatenation.
        where_clauses: list[ClauseElement] = []
        fvp = getattr(self, "fetch_values_predicate", None)
        if fvp:
            # Apply Jinja template processing so expressions like
            # ``{{ current_username() }}`` or ``{{ current_user_id() }}``
            # are resolved at query time.  Matches original
            # ``helpers.values_for_column`` (line 1586) which calls
            # ``self.get_fetch_values_predicate(template_processor=tp)``
            # where the processor runs ``process_template(clause)``.
            # ``process_template`` is pure CPU/Jinja work with no I/O,
            # so calling it from async code is safe; we still wrap it
            # in ``asyncio.to_thread`` to match how other long-running
            # sync helpers are invoked from the async pipeline.
            fvp_processed = await asyncio.to_thread(processor.process_template, fvp)
            where_clauses.append(sa_text(f"({fvp_processed})"))

        if rls_filters:
            for rls_clause in rls_filters:
                if isinstance(rls_clause, str):
                    stripped = rls_clause.strip()
                    if not stripped:
                        continue
                    rls_clause = sa_text(stripped)
                if isinstance(rls_clause, ClauseElement):
                    where_clauses.append(rls_clause)
                else:
                    logger.warning(
                        "Unknown RLS clause type %s, skipping",
                        type(rls_clause).__name__,
                    )

        if where_clauses:
            qry = qry.where(and_(*where_clauses))

        if limit:
            qry = qry.limit(int(limit))

        # Compile the entire AST through the database's dialect — single
        # pass produces engine-correct identifier quoting and dialect-
        # specific translation for SELECT, FROM, WHERE, RLS, and LIMIT.
        dialect = self.database.get_dialect()
        sql = str(
            qry.compile(
                dialect=dialect,
                compile_kwargs={"literal_binds": True},
            )
        )

        # Prepend the CTE (if any) so engines that disallow CTEs
        # inside a subquery still execute a well-formed statement.
        sql = self._apply_cte(sql, cte_sql)

        # Apply the user-defined ``SQL_QUERY_MUTATOR`` config hook — 1:1 with
        # original ``helpers.values_for_column`` (line 1594)
        # ``self.database.mutate_sql_based_on_config(sql)``. Sync/CPU work, so
        # dispatched to a worker thread like the other sync DB helpers.
        sql = await asyncio.to_thread(self.database.mutate_sql_based_on_config, sql)

        # ``literal_binds`` rendering doubles literal percent signs on dialects
        # whose identifier preparer escapes them (``%`` -> ``%%``); undo this
        # so the executed SQL matches the user's intent — 1:1 with original
        # ``helpers.values_for_column`` (lines 1597-1598).
        if dialect.identifier_preparer._double_percents:  # noqa: SLF001
            sql = sql.replace("%%", "%")

        df = await self._execute_sql(sql)
        if df.empty or "column_values" not in df.columns:
            return []
        values = df["column_values"].replace({np.nan: None}).tolist()
        return values

# ---------------------------------------------------------------------------
# RowLevelSecurityFilter
# ---------------------------------------------------------------------------


class RowLevelSecurityFilter(Base, AuditMixinNullable):
    """A row-level security filter applied to datasets.

    Schema mirrors ``superset_old/connectors/sqla/models.py`` exactly so
    existing Apache Superset metadata databases work unchanged.
    """

    __tablename__ = "row_level_security_filters"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text)
    # ``native_enum=False`` keeps the column as VARCHAR in PostgreSQL,
    # matching the production state of Apache Superset 6.0.  Without this,
    # asyncpg generates ``$1::filter_type_enum`` casts that fail when the
    # ENUM type doesn't exist in the metadata DB; psycopg2 (used by upstream
    # Apache Superset) sent values as plain text and PG implicit-converted
    # them.  See similar reasoning on ``Tag.type`` / ``TaggedObject.object_type``.
    filter_type = Column(
        SAEnum(
            *[ft.value for ft in _RowLevelSecurityFilterType],
            name="filter_type_enum",
            native_enum=False,
        ),
    )
    group_key = Column(String(255), nullable=True)
    clause = Column(MediumText(), nullable=False)

    # ``backref`` mirrors the original — gives ``Role.row_level_security_filters``
    # and ``SqlaTable.row_level_security_filters``. ``overlaps="table"`` on
    # ``tables`` silences the SQLAlchemy warning caused by the same column
    # being referenced from ``SqlaTable.table_name`` aliases.
    roles = relationship(
        "Role",
        secondary=RLSFilterRoles,
        backref="row_level_security_filters",
    )
    tables = relationship(
        "SqlaTable",
        secondary=RLSFilterTables,
        backref="row_level_security_filters",
        overlaps="table",
    )
