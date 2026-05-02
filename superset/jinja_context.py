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
"""Defines the templating context for SQL Lab.

Migrated from superset_old/jinja_context.py — Flask dependencies removed,
user context resolved via context-vars (set_current_user / get_current_user).
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache, partial
from typing import Any, Callable, cast, TYPE_CHECKING, TypedDict, Union

import dateutil
from jinja2 import DebugUndefined, Environment, TemplateSyntaxError
from jinja2.exceptions import SecurityError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.expression import bindparam
from sqlalchemy.types import String

from superset.constants import LRU_CACHE_MAX_SIZE, NO_TIME_RANGE
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    DatasetNotFoundError,
    SupersetSyntaxErrorException,
    SupersetTemplateException,
)
from superset.sql.parse import Table
from superset.utils import json
from superset.utils.core import (
    AdhocFilterClause,
    convert_legacy_filters_into_adhoc,
    FilterOperator,
    get_current_user,
    get_user_email,
    get_user_id,
    get_username,
    merge_extra_filters,
)
from superset.utils.feature_flags import feature_flag_manager

if TYPE_CHECKING:
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.core import Database
    from superset.models.sql_lab import Query

logger = logging.getLogger(__name__)

NONE_TYPE = type(None).__name__
ALLOWED_TYPES = (
    NONE_TYPE,
    "bool",
    "str",
    "unicode",
    "int",
    "long",
    "float",
    "list",
    "dict",
    "tuple",
    "set",
    "TimeFilter",
)
COLLECTION_TYPES = ("list", "dict", "tuple", "set")


# ---------------------------------------------------------------------------
# Config access (replaces Flask current_app.config)
# ---------------------------------------------------------------------------
_settings_instance: Any = None


def _get_config(key: str, default: Any = None) -> Any:
    """Read a config value from SupersetSettings (lazy singleton)."""
    global _settings_instance  # noqa: PLW0603
    if _settings_instance is None:
        from superset.config import SupersetSettings

        _settings_instance = SupersetSettings()  # type: ignore[call-arg]
    mapped = getattr(_settings_instance, key.lower(), None)
    if mapped is not None:
        return mapped
    return default


@lru_cache(maxsize=LRU_CACHE_MAX_SIZE)
def context_addons() -> dict[str, Any]:
    return _get_config("JINJA_CONTEXT_ADDONS", {})


class Filter(TypedDict):
    op: str
    col: str
    val: Union[None, Any, list[Any]]


@dataclass
class TimeFilter:
    """Container for temporal filter."""

    from_expr: str | None
    to_expr: str | None
    time_range: str | None


# ---------------------------------------------------------------------------
# form_data context var — controllers must call set_form_data() before
# rendering templates so that url_param / get_filters / get_time_filter
# can access the current request's form data.
# ---------------------------------------------------------------------------
_form_data_ctx: ContextVar[dict[str, Any] | None] = ContextVar(
    "_form_data_ctx", default=None
)


def set_form_data(form_data: dict[str, Any]) -> None:
    """Set form_data for the current async context (call from controller)."""
    _form_data_ctx.set(form_data)


def get_form_data() -> dict[str, Any]:
    """Return current form_data or empty dict."""
    return _form_data_ctx.get(None) or {}


class ExtraCache:
    """
    Dummy class that exposes a method used to store additional values used in
    calculation of query object cache keys.
    """

    regex = re.compile(
        r"(\{\{|\{%)[^{}]*?("
        r"current_user_id\([^()]*\)|"
        r"current_username\([^()]*\)|"
        r"current_user_email\([^()]*\)|"
        r"current_user_rls_rules\([^()]*\)|"
        r"current_user_roles\([^()]*\)|"
        r"cache_key_wrapper\([^()]*\)|"
        r"url_param\([^()]*\)"
        r")"
        r"[^{}]*?(\}\}|%\})"
    )

    def __init__(
        self,
        extra_cache_keys: list[Any] | None = None,
        applied_filters: list[str] | None = None,
        removed_filters: list[str] | None = None,
        database: "Database | None" = None,
        dialect: Dialect | None = None,
        table: "SqlaTable | None" = None,
        form_data: dict[str, Any] | None = None,
    ):
        self.extra_cache_keys = extra_cache_keys
        self.applied_filters = applied_filters if applied_filters is not None else []
        self.removed_filters = removed_filters if removed_filters is not None else []
        self.database = database
        self.dialect = dialect
        self.table = table
        self.form_data = form_data if form_data is not None else get_form_data()

    def current_user_id(self, add_to_cache_keys: bool = True) -> int | None:
        if user_id := get_user_id():
            if add_to_cache_keys:
                self.cache_key_wrapper(user_id)
            return user_id
        return None

    def current_username(self, add_to_cache_keys: bool = True) -> str | None:
        if username := get_username():
            if add_to_cache_keys:
                self.cache_key_wrapper(username)
            return username
        return None

    def current_user_email(self, add_to_cache_keys: bool = True) -> str | None:
        if email_address := get_user_email():
            if add_to_cache_keys:
                self.cache_key_wrapper(email_address)
            return email_address
        return None

    def current_user_roles(self, add_to_cache_keys: bool = True) -> list[str] | None:
        """
        Return sorted role names for the current user.

        Original Superset uses security_manager.get_user_roles() which handles
        guest/embedded user roles. In Liteset, we read from the user object
        which should have roles populated by the auth middleware. Guest role
        handling is done at the middleware/guard layer.
        """
        try:
            user = get_current_user()
            if not user:
                return None
            roles = getattr(user, "roles", None)
            if not roles:
                return None
            user_roles = sorted([role.name for role in roles])
            if not user_roles:
                return None
            if add_to_cache_keys:
                self.cache_key_wrapper(json.dumps(user_roles))
            return user_roles
        except Exception:
            return None

    def current_user_rls_rules(self) -> list[str] | None:
        """
        Return row level security rules for the current user and dataset.

        Original Superset calls security_manager.get_rls_filters(table) and
        security_manager.get_guest_rls_filters(table). In Liteset, RLS is
        handled by AsyncSecurityManager. Since template rendering is synchronous,
        we use a sync fallback: read from the rls_rules context var that should
        be set by the controller before rendering.
        """
        if not self.table:
            return None
        rls_rules = getattr(self, "_rls_rules", None)
        if rls_rules is None:
            return None
        if not rls_rules:
            return None
        self.cache_key_wrapper(json.dumps(rls_rules))
        return rls_rules

    def cache_key_wrapper(self, key: Any) -> Any:
        if self.extra_cache_keys is not None:
            self.extra_cache_keys.append(key)
        return key

    def url_param(
        self,
        param: str,
        default: str | None = None,
        add_to_cache_keys: bool = True,
        escape_result: bool = True,
    ) -> str | None:
        """
        Read a url or post parameter and use it in your SQL Lab query.

        In the original Superset this first checks Flask request.args, then
        falls back to form_data.url_params. In Liteset the controller must
        merge request query_params into form_data["url_params"] before
        rendering templates.
        """
        url_params = self.form_data.get("url_params") or {}

        # Check url_params from form_data (includes request.query_params
        # merged by controller)
        result = url_params.get(param, default)
        if result and escape_result and self.dialect:
            result = String().literal_processor(  # type: ignore[no-untyped-call]
                dialect=self.dialect
            )(value=result)[1:-1]
        if add_to_cache_keys:
            self.cache_key_wrapper(result)
        return result

    def filter_values(
        self, column: str, default: str | None = None, remove_filter: bool = False
    ) -> list[Any]:
        return_val: list[Any] = []
        filters = self.get_filters(column, remove_filter)
        for flt in filters:
            val = flt.get("val")
            if isinstance(val, list):
                return_val.extend(val)
            elif val:
                return_val.append(val)

        if (not return_val) and default:
            return_val = [default]

        return return_val

    def get_filters(self, column: str, remove_filter: bool = False) -> list[Filter]:
        form_data = dict(self.form_data)  # copy to avoid mutation
        convert_legacy_filters_into_adhoc(form_data)
        merge_extra_filters(form_data)

        filters: list[Filter] = []

        for flt in form_data.get("adhoc_filters", []):
            val: Union[Any, list[Any]] = flt.get("comparator")
            op: str = flt["operator"].upper() if flt.get("operator") else None  # type: ignore
            if (
                flt.get("expressionType") == "SIMPLE"
                and flt.get("clause") == "WHERE"
                and flt.get("subject") == column
                and (
                    val
                    or op
                    in (
                        FilterOperator.IS_NULL,
                        FilterOperator.IS_NOT_NULL,
                    )
                )
            ):
                if remove_filter:
                    if column not in self.removed_filters:
                        self.removed_filters.append(column)
                if column not in self.applied_filters:
                    self.applied_filters.append(column)

                if op in (
                    FilterOperator.IN,
                    FilterOperator.NOT_IN,
                ) and not isinstance(val, list):
                    val = [val]

                filters.append({"op": op, "col": column, "val": val})

        return filters

    def get_time_filter(
        self,
        column: str | None = None,
        default: str | None = None,
        target_type: str | None = None,
        strftime: str | None = None,
        remove_filter: bool = False,
    ) -> TimeFilter:
        from superset.utils.date import get_since_until_from_time_range

        form_data = dict(self.form_data)  # copy to avoid mutation
        convert_legacy_filters_into_adhoc(form_data)
        merge_extra_filters(form_data)
        time_range = form_data.get("time_range")
        if column:
            flt: AdhocFilterClause | None = next(
                (
                    flt
                    for flt in form_data.get("adhoc_filters", [])
                    if flt["operator"] == FilterOperator.TEMPORAL_RANGE
                    and flt["subject"] == column
                ),
                None,
            )
            if flt:
                if remove_filter and column not in self.removed_filters:
                    self.removed_filters.append(column)
                if column not in self.applied_filters:
                    self.applied_filters.append(column)

                time_range = cast(str, flt["comparator"])
                if not target_type and self.table:
                    target_type = self.table.columns_types.get(column)

        time_range = time_range or NO_TIME_RANGE
        if time_range == NO_TIME_RANGE and default:
            time_range = default
        from_expr, to_expr = get_since_until_from_time_range(time_range)

        def _format_dttm(dttm: datetime | None) -> str | None:
            if strftime and dttm:
                return dttm.strftime(strftime)
            return (
                self.database.db_engine_spec.convert_dttm(target_type or "", dttm)
                if self.database and dttm
                else None
            )

        return TimeFilter(
            from_expr=_format_dttm(from_expr),
            to_expr=_format_dttm(to_expr),
            time_range=time_range,
        )


def safe_proxy(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return_value = func(*args, **kwargs)
    value_type = type(return_value).__name__
    if value_type not in ALLOWED_TYPES:
        raise SupersetTemplateException(
            f"Unsafe return type for function {func.__name__}: {value_type}"
        )
    if value_type in COLLECTION_TYPES:
        try:
            return_value = json.loads(json.dumps(return_value))
        except TypeError as ex:
            raise SupersetTemplateException(
                f"Unsupported return value for method {func.__name__}"
            ) from ex

    return return_value


def validate_context_types(context: dict[str, Any]) -> dict[str, Any]:
    for key in context:
        arg_type = type(context[key]).__name__
        if arg_type not in ALLOWED_TYPES and key not in context_addons():
            if arg_type == "partial" and context[key].func.__name__ == "safe_proxy":
                continue
            raise SupersetTemplateException(
                f"Unsafe template value for key {key}: {arg_type}"
            )
        if arg_type in COLLECTION_TYPES:
            try:
                context[key] = json.loads(json.dumps(context[key]))
            except TypeError as ex:
                raise SupersetTemplateException(
                    f"Unsupported template value for key {key}"
                ) from ex

    return context


def validate_template_context(
    engine: str | None, context: dict[str, Any]
) -> dict[str, Any]:
    if engine and engine in context:
        engine_context = validate_context_types(context.pop(engine))
        valid_context = validate_context_types(context)
        valid_context[engine] = engine_context
        return valid_context

    return validate_context_types(context)


class WhereInMacro:
    def __init__(self, dialect: Dialect):
        self.dialect = dialect

    def __call__(
        self,
        values: list[Any],
        mark: str | None = None,
        default_to_none: bool = False,
    ) -> str | None:
        binds: list[Any] = [
            bindparam(f"value_{i}", value) for i, value in enumerate(values)
        ]
        string_representations = [
            str(
                bind.compile(
                    dialect=self.dialect, compile_kwargs={"literal_binds": True}
                )
            )
            for bind in binds
        ]
        joined_values = ", ".join(string_representations)
        result = (
            f"({joined_values})" if (joined_values or not default_to_none) else None
        )

        if mark and result:
            result += (
                "\n-- WARNING: the `mark` parameter was removed from the `where_in` "
                "macro for security reasons\n"
            )

        return result


def to_datetime(
    value: str | None,
    format: str = "%Y-%m-%d %H:%M:%S",  # noqa: A002
) -> datetime | None:
    if not value:
        return None
    value = value.strip("'\"")
    return datetime.strptime(value, format)


class BaseTemplateProcessor:
    """Base class for database-specific jinja context"""

    engine: str | None = None

    def __init__(
        self,
        database: "Database",
        query: "Query | None" = None,
        table: "SqlaTable | None" = None,
        extra_cache_keys: list[Any] | None = None,
        removed_filters: list[str] | None = None,
        applied_filters: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._database = database
        self._query = query
        self._schema = None
        if query and query.schema:
            self._schema = query.schema
        elif table:
            self._schema = table.schema
        self._table = table
        self._extra_cache_keys = extra_cache_keys
        self._applied_filters = applied_filters
        self._removed_filters = removed_filters
        self._context: dict[str, Any] = {}
        self.env: Environment = SandboxedEnvironment(undefined=DebugUndefined)
        self.set_context(**kwargs)

        # custom filters
        self.env.filters["where_in"] = WhereInMacro(database.get_dialect())
        self.env.filters["to_datetime"] = to_datetime

    def set_context(self, **kwargs: Any) -> None:
        self._context.update(kwargs)
        self._context.update(context_addons())

    def get_context(self) -> dict[str, Any]:
        return self._context.copy()

    def process_template(self, sql: str, **kwargs: Any) -> str:
        """Processes a sql template

        >>> sql = "SELECT '{{ datetime(2017, 1, 1).isoformat() }}'"
        >>> process_template(sql)
        "SELECT '2017-01-01T00:00:00'"
        """
        try:
            template = self.env.from_string(sql)
        except (
            TemplateSyntaxError,
            SecurityError,
            UndefinedError,
        ) as ex:
            error_msg = str(ex)
            exception_type = type(ex).__name__

            message = f"Jinja2 template error ({exception_type}): {error_msg}"
            line_number = getattr(ex, "lineno", None)

            logger.warning(
                "Jinja2 template client error",
                extra={
                    "error_message": error_msg,
                    "template_snippet": sql[:200] if sql else None,
                    "template_length": len(sql) if sql else 0,
                    "line_number": line_number,
                    "error_type": "CLIENT_TEMPLATE_ERROR",
                    "exception_type": exception_type,
                },
                exc_info=False,
            )

            error = SupersetError(
                message=message,
                error_type=SupersetErrorType.GENERIC_COMMAND_ERROR,
                level=ErrorLevel.ERROR,
                extra={
                    "template": sql[:500],
                    "line": line_number,
                    "exception_type": exception_type,
                },
            )

            raise SupersetSyntaxErrorException([error]) from ex  # type: ignore[list-item]
        except Exception as ex:
            error_msg = str(ex)
            exception_type = type(ex).__name__

            message = f"Internal Jinja2 template error ({exception_type}): {error_msg}"

            logger.error(
                "Jinja2 template server error",
                extra={
                    "error_message": error_msg,
                    "template_snippet": sql[:200] if sql else None,
                    "template_length": len(sql) if sql else 0,
                    "error_type": "SERVER_TEMPLATE_ERROR",
                    "exception_type": exception_type,
                },
                exc_info=True,
            )

            raise SupersetTemplateException(message) from ex

        kwargs.update(self._context)
        context = validate_template_context(self.engine, kwargs)

        try:
            return template.render(context)
        except RecursionError as ex:
            raise SupersetTemplateException(
                "Infinite recursion detected in template"
            ) from ex


class JinjaTemplateProcessor(BaseTemplateProcessor):
    @staticmethod
    def _parse_datetime(dttm: str) -> datetime | None:
        try:
            return dateutil.parser.parse(dttm)
        except dateutil.parser.ParserError:
            return None

    def set_context(self, **kwargs: Any) -> None:
        super().set_context(**kwargs)
        extra_cache = ExtraCache(
            extra_cache_keys=self._extra_cache_keys,
            applied_filters=self._applied_filters,
            removed_filters=self._removed_filters,
            database=self._database,
            dialect=self._database.get_dialect(),
            table=self._table,
        )

        from_dttm = (
            self._parse_datetime(dttm)
            if (dttm := self._context.get("from_dttm"))
            else None
        )
        to_dttm = (
            self._parse_datetime(dttm)
            if (dttm := self._context.get("to_dttm"))
            else None
        )

        dataset_macro_with_context = partial(
            dataset_macro,
            from_dttm=from_dttm,
            to_dttm=to_dttm,
        )

        self._context.update(
            {
                "url_param": partial(safe_proxy, extra_cache.url_param),
                "current_user_id": partial(safe_proxy, extra_cache.current_user_id),
                "current_username": partial(safe_proxy, extra_cache.current_username),
                "current_user_email": partial(
                    safe_proxy, extra_cache.current_user_email
                ),
                "current_user_roles": partial(
                    safe_proxy, extra_cache.current_user_roles
                ),
                "current_user_rls_rules": partial(
                    safe_proxy, extra_cache.current_user_rls_rules
                ),
                "cache_key_wrapper": partial(safe_proxy, extra_cache.cache_key_wrapper),
                "filter_values": partial(safe_proxy, extra_cache.filter_values),
                "get_filters": partial(safe_proxy, extra_cache.get_filters),
                "dataset": partial(safe_proxy, dataset_macro_with_context),
                "get_time_filter": partial(safe_proxy, extra_cache.get_time_filter),
            }
        )

        self._context["metric"] = partial(
            safe_proxy,
            metric_macro,
            self.env,
            self._context,
        )


class NoOpTemplateProcessor(BaseTemplateProcessor):
    def process_template(self, sql: str, **kwargs: Any) -> str:
        return str(sql)


class PrestoTemplateProcessor(JinjaTemplateProcessor):
    """Presto Jinja context

    The methods described here are namespaced under ``presto`` in the
    jinja context as in ``SELECT '{{ presto.some_macro_call() }}'``
    """

    engine = "presto"

    def set_context(self, **kwargs: Any) -> None:
        super().set_context(**kwargs)
        self._context[self.engine] = {
            "first_latest_partition": partial(safe_proxy, self.first_latest_partition),
            "latest_partitions": partial(safe_proxy, self.latest_partitions),
            "latest_sub_partition": partial(safe_proxy, self.latest_sub_partition),
            "latest_partition": partial(safe_proxy, self.latest_partition),
        }

    @staticmethod
    def _schema_table(table_name: str, schema: str | None) -> tuple[str, str | None]:
        if "." in table_name:
            schema, table_name = table_name.split(".")
        return table_name, schema

    def first_latest_partition(self, table_name: str) -> str | None:
        latest_partitions = self.latest_partitions(table_name)
        return latest_partitions[0] if latest_partitions else None

    def latest_partitions(self, table_name: str) -> list[str] | None:
        from superset.db_engine_specs.presto import PrestoEngineSpec

        table_name, schema = self._schema_table(table_name, self._schema)
        return cast(PrestoEngineSpec, self._database.db_engine_spec).latest_partition(  # type: ignore[attr-defined]
            database=self._database, table=Table(table_name, schema)
        )[1]

    def latest_sub_partition(self, table_name: str, **kwargs: Any) -> Any:
        table_name, schema = self._schema_table(table_name, self._schema)

        from superset.db_engine_specs.presto import PrestoEngineSpec

        return cast(
            PrestoEngineSpec, self._database.db_engine_spec
        ).latest_sub_partition(  # type: ignore[attr-defined]
            database=self._database, table=Table(table_name, schema), **kwargs
        )

    latest_partition = first_latest_partition


class HiveTemplateProcessor(PrestoTemplateProcessor):
    engine = "hive"


class SparkTemplateProcessor(HiveTemplateProcessor):
    engine = "spark"

    def process_template(self, sql: str, **kwargs: Any) -> str:
        template = self.env.from_string(sql)
        kwargs.update(self._context)

        # Backwards compatibility if migrating from Hive.
        context = validate_template_context(self.engine, kwargs)
        context["hive"] = context["spark"]
        return template.render(context)


class TrinoTemplateProcessor(PrestoTemplateProcessor):
    engine = "trino"

    def process_template(self, sql: str, **kwargs: Any) -> str:
        template = self.env.from_string(sql)
        kwargs.update(self._context)

        # Backwards compatibility if migrating from Presto.
        context = validate_template_context(self.engine, kwargs)
        context["presto"] = context["trino"]
        return template.render(context)


DEFAULT_PROCESSORS = {
    "presto": PrestoTemplateProcessor,
    "hive": HiveTemplateProcessor,
    "spark": SparkTemplateProcessor,
    "trino": TrinoTemplateProcessor,
}


@lru_cache(maxsize=LRU_CACHE_MAX_SIZE)
def get_template_processors() -> dict[str, Any]:
    processors = _get_config("CUSTOM_TEMPLATE_PROCESSORS", {})
    for engine, processor in DEFAULT_PROCESSORS.items():
        if engine not in processors:
            processors[engine] = processor

    return processors


def get_template_processor(
    database: "Database",
    table: "SqlaTable | None" = None,
    query: "Query | None" = None,
    **kwargs: Any,
) -> BaseTemplateProcessor:
    if feature_flag_manager.is_feature_enabled("ENABLE_TEMPLATE_PROCESSING"):
        template_processor = get_template_processors().get(
            database.backend, JinjaTemplateProcessor
        )
    else:
        template_processor = NoOpTemplateProcessor
    return template_processor(database=database, table=table, query=query, **kwargs)


# ---------------------------------------------------------------------------
# Sync dataset lookup helper
#
# The original Superset uses DatasetDAO.find_by_id() which is a synchronous
# class-method backed by Flask-SQLAlchemy. In Liteset the DAO layer is fully
# async. For synchronous template rendering we use a direct sync query.
# ---------------------------------------------------------------------------
def _sync_find_dataset(dataset_id: int) -> Any:
    """Synchronously find a dataset by ID using the sync Alembic engine."""
    from superset.config import SupersetSettings
    from superset.models.connectors import SqlaTable

    settings = SupersetSettings()  # type: ignore[call-arg]
    sync_uri = str(settings.sqlalchemy_database_uri)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(sync_uri)
    with Session(engine) as session:
        return (
            session.execute(select(SqlaTable).where(SqlaTable.id == dataset_id))
            .scalars()
            .one_or_none()
        )


def dataset_macro(
    dataset_id: int,
    include_metrics: bool = False,
    columns: list[str] | None = None,
    from_dttm: datetime | None = None,
    to_dttm: datetime | None = None,
) -> str:
    """
    Given a dataset ID, return the SQL that represents it.

    The generated SQL includes all columns (including computed) by default.
    """
    dataset = _sync_find_dataset(dataset_id)
    if not dataset:
        raise DatasetNotFoundError(f"Dataset {dataset_id} not found!")

    columns = columns or [column.column_name for column in dataset.columns]
    metrics = [metric.metric_name for metric in dataset.metrics]
    query_obj = {
        "is_timeseries": False,
        "filter": [],
        "metrics": metrics if include_metrics else None,
        "columns": columns,
        "from_dttm": from_dttm,
        "to_dttm": to_dttm,
    }
    sqla_query = dataset.get_query_str_extended(query_obj, mutate=False)
    sql = sqla_query.sql
    return f"(\n{sql}\n) AS dataset_{dataset_id}"


def get_dataset_id_from_context(metric_key: str) -> int:
    """
    Retrieves the Dataset ID from the template context.

    In the original Superset this reads from Flask request context (JSON body,
    form data, request args, g.form_data). In Liteset we first read from the
    request-scoped form_data ContextVar populated by the request_context
    middleware, then fall back to :func:`superset.utils.core.get_form_data`
    which mirrors the original ``g.form_data`` slot — this is the path used
    by Celery tasks (``async_queries``) and the ``warm_up_cache`` command,
    neither of which run inside an HTTP request.
    """
    # Lazy import avoids a top-level cycle (utils.core imports nothing
    # from jinja_context, but downstream callers import from utils.core
    # all over the place during module initialisation).
    from superset.utils.core import get_form_data as get_task_form_data

    exc_message = (
        f"Please specify the Dataset ID for the ``{metric_key}`` "
        f"metric in the Jinja macro."
    )

    form_data = get_form_data() or get_task_form_data()
    if not form_data:
        raise SupersetTemplateException(exc_message)

    # Check datasource field (can be dict or "id__type" string)
    if datasource_info := form_data.get("datasource"):
        if isinstance(datasource_info, dict):
            if ds_id := datasource_info.get("id"):
                return ds_id
        elif isinstance(datasource_info, str) and "__" in datasource_info:
            return int(datasource_info.split("__")[0])

    # Check queries[0].url_params.datasource_id
    queries = form_data.get("queries", [{}])
    if queries:
        url_params = queries[0].get("url_params", {})
        if dataset_id := url_params.get("datasource_id"):
            return int(dataset_id)

        # Check slice_id -> chart -> datasource_id
        slice_id = form_data.get("slice_id") or url_params.get("slice_id")
        if slice_id:
            # Synchronous fallback for chart lookup
            from sqlalchemy import create_engine
            from sqlalchemy.orm import Session

            from superset.config import SupersetSettings
            from superset.models.slice import Slice

            settings = SupersetSettings()  # type: ignore[call-arg]
            engine = create_engine(str(settings.sqlalchemy_database_uri))
            with Session(engine) as session:
                chart = (
                    session.execute(select(Slice).where(Slice.id == int(slice_id)))
                    .scalars()
                    .one_or_none()
                )
                if chart:
                    return int(chart.datasource_id)

    raise SupersetTemplateException(exc_message)


def metric_macro(
    env: Environment,
    context: dict[str, Any],
    metric_key: str,
    dataset_id: int | None = None,
) -> str:
    """Given a metric key, returns its syntax."""
    if not dataset_id:
        dataset_id = get_dataset_id_from_context(metric_key)

    # Original: DatasetDAO.find_by_id(dataset_id, skip_base_filter=is_guest)
    # In Liteset guest user handling is done at the controller/guard layer,
    # so we do an unfiltered lookup here.
    dataset = _sync_find_dataset(dataset_id)
    if not dataset:
        raise DatasetNotFoundError(f"Dataset ID {dataset_id} not found.")

    metrics: dict[str, str] = {
        metric.metric_name: metric.expression for metric in dataset.metrics
    }
    if metric_key not in metrics:
        raise SupersetTemplateException(
            f"Metric ``{metric_key}`` not found in {dataset.table_name}."
        )

    definition = metrics[metric_key]
    template = env.from_string(definition)
    definition = template.render(context)

    return definition
