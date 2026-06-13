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

Migrated from superset_old/jinja_context.py — legacy WSGI dependencies
gone, user context resolved via context-vars (set_current_user /
get_current_user).
"""

from __future__ import annotations

import logging
import re
import threading as _threading
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
from sqlalchemy.exc import StatementError
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
    get_current_request,
    get_current_user,
    get_user_email,
    get_user_id,
    get_username,
    merge_extra_filters,
)
from superset.utils.feature_flags import feature_flag_manager

if TYPE_CHECKING:
    from superset.models.connectors import SqlaTable
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
# Config access (replaces the upstream current_app.config)
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

        Original Superset uses security_manager.get_user_roles() which, for
        anonymous users, returns ``[self.get_public_role()]`` when
        ``AUTH_ROLE_PUBLIC`` is configured. We reproduce that behaviour here:
        when no user is present (anonymous) and ``AUTH_ROLE_PUBLIC`` is set,
        we return ``[public_role_name]``.
        """
        try:
            user = get_current_user()
            # Anonymous detection mirrors the upstream get_user_roles'
            # ``is_anonymous`` branch: the middleware stores a truthy
            # ``UnauthenticatedUser``
            # (is_authenticated=False) in the ContextVar, so a plain
            # truthiness check would skip the public-role branch. ORM users
            # have no ``is_authenticated`` attribute -> default True.
            if not user or getattr(user, "is_authenticated", True) is False:
                # Mirror original: anonymous users get the public role
                # if AUTH_ROLE_PUBLIC is configured. The upstream
                # get_public_role()
                # queries the DB (sqla/manager.py:717-722) — a configured but
                # DELETED role yields None, then ``None.name`` raises inside
                # the original's try/except → None. Reproduce the existence
                # check instead of trusting the raw config string.
                public_role = _get_config("AUTH_ROLE_PUBLIC")
                if not public_role or not _sync_role_exists(str(public_role)):
                    return None
                user_roles = [public_role]
                if add_to_cache_keys:
                    self.cache_key_wrapper(json.dumps(user_roles))
                return user_roles
            roles = getattr(user, "roles", None) or []
            role_names: set[str] = {
                role.name for role in roles if hasattr(role, "name")
            }
            # Mirror the upstream base get_user_roles:
            #   user.roles + [role for group in user.groups for role in group.roles]
            # The group term is resolved via a sync query (same pattern as
            # _sync_get_rls_rules) since Jinja rendering is synchronous.
            user_id = getattr(user, "id", None)
            if user_id is not None:
                try:
                    role_names.update(_sync_get_user_group_role_names(user_id))
                except Exception:  # noqa: BLE001
                    logger.debug("Failed to load user group role names", exc_info=True)
            if not role_names:
                return None
            user_roles = sorted(role_names)
            if add_to_cache_keys:
                self.cache_key_wrapper(json.dumps(user_roles))
            return user_roles
        except Exception:
            return None

    def current_user_rls_rules(self) -> list[str] | None:
        """
        Return row level security rules for the current user and dataset.

        Ported 1:1 from
        superset_old/jinja_context.py::ExtraCache.current_user_rls_rules.
        The original called security_manager.get_rls_filters(self.table) (sync,
        because the original SM was sync). In Liteset we reproduce the
        same query synchronously via _sync_get_rls_rules which uses a cached
        sync SQLAlchemy engine — acceptable since Jinja rendering is sync.
        """
        if not self.table:
            return None
        user = get_current_user()
        try:
            rls_rules = _sync_get_rls_rules(self.table, user)
        except Exception:  # noqa: BLE001
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

        1:1 with upstream: first checks request.query_params (Litestar equivalent
        of the upstream request.args), then falls back to
        form_data["url_params"].
        """
        # Mirror upstream: ``if has_request_context() and request.args.get(param)``
        _request = get_current_request()
        if _request is not None:
            _query_params = getattr(_request, "query_params", None)
            if _query_params is not None:
                _val = _query_params.get(param)
                if _val:
                    return _val

        url_params = self.form_data.get("url_params") or {}
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
            # Support both camelCase and snake_case payload conventions
            # (msgspec ``rename="camel"`` decoded structs use snake_case).
            expression_type = flt.get("expressionType") or flt.get("expression_type")
            if (
                expression_type == "SIMPLE"
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
            UnicodeError,
            UnicodeDecodeError,
            UnicodeEncodeError,
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

            raise SupersetSyntaxErrorException([error]) from ex
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
        return cast(PrestoEngineSpec, self._database.db_engine_spec).latest_partition(
            database=self._database, table=Table(table_name, schema)
        )[1]

    def latest_sub_partition(self, table_name: str, **kwargs: Any) -> Any:
        table_name, schema = self._schema_table(table_name, self._schema)

        from superset.db_engine_specs.presto import PrestoEngineSpec

        return cast(
            PrestoEngineSpec, self._database.db_engine_spec
        ).latest_sub_partition(
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
# class-method backed by the upstream ORM integration. In Liteset the DAO
# layer is fully async. For synchronous template rendering we use a direct
# sync query.
#
# P1-3 fix: cache the engine at module level (keyed by DB URI) so we create
# at most one engine per process — avoiding per-call connection pool leaks.
# ---------------------------------------------------------------------------
_sync_engine_lock = _threading.Lock()
_sync_engine_cache: dict[str, Any] = {}


def _get_sync_engine() -> Any:
    """Return (or create) the module-level sync SQLAlchemy engine.

    The engine is keyed by the database URI and created at most once per
    process, protected by a lock for thread safety in Celery workers.
    """
    from sqlalchemy import create_engine as _create_engine

    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    sync_uri = str(settings.sqlalchemy_database_uri)

    if sync_uri not in _sync_engine_cache:
        with _sync_engine_lock:
            # Double-checked locking
            if sync_uri not in _sync_engine_cache:
                _sync_engine_cache[sync_uri] = _create_engine(
                    sync_uri,
                    pool_pre_ping=True,
                )
    return _sync_engine_cache[sync_uri]


def _sync_find_dataset(dataset_id: int) -> Any:
    """Synchronously find a dataset by ID using the shared sync engine.

    Mirrors ``BaseDAO.find_by_id`` (superset_old/daos/base.py lines 68-72):
    ``StatementError`` (e.g. from a non-numeric *dataset_id* string reaching the
    DB) is caught and ``None`` is returned, so callers raise a 404-level error
    (``DatasetNotFoundError``) instead of propagating a 500.
    """
    from sqlalchemy.orm import Session

    from superset.models.connectors import SqlaTable

    engine = _get_sync_engine()
    try:
        with Session(engine) as session:
            return (
                session.execute(select(SqlaTable).where(SqlaTable.id == dataset_id))
                .scalars()
                .one_or_none()
            )
    except StatementError:
        return None


_DATABASE_PERM_RE = re.compile(r"^\[.+\]\.\(id:(?P<id>\d+)\)$")


def _collect_role_perm_rows(role_ids: list[int]) -> list[Any]:
    """Query permission rows for the given role IDs from the sync engine."""
    from sqlalchemy.orm import Session

    from superset.models.security import (
        ab_permission_view_role,
        Permission,
        PermissionView,
        ViewMenu,
    )

    engine = _get_sync_engine()
    with Session(engine) as session:
        return list(
            session.execute(
                select(Permission.name, ViewMenu.name)
                .select_from(ab_permission_view_role)
                .join(
                    PermissionView,
                    PermissionView.id == ab_permission_view_role.c.permission_view_id,
                )
                .join(Permission, Permission.id == PermissionView.permission_id)
                .join(ViewMenu, ViewMenu.id == PermissionView.view_menu_id)
                .where(ab_permission_view_role.c.role_id.in_(role_ids))
            ).all()
        )


def _classify_perm_rows(
    rows: list[Any],
) -> tuple[bool, set[int], set[str], set[str], set[str]]:
    """Classify permission rows into access sets.

    Returns ``(has_global_access, db_ids, datasource_perms, schema_perms,
    catalog_perms)``.  ``has_global_access`` is ``True`` when any row carries
    ``all_database_access`` or ``all_datasource_access``.
    """
    db_ids: set[int] = set()
    datasource_perms: set[str] = set()
    schema_perms: set[str] = set()
    catalog_perms: set[str] = set()
    for perm_name, vm_name in rows:
        if perm_name in ("all_database_access", "all_datasource_access"):
            return True, db_ids, datasource_perms, schema_perms, catalog_perms
        if perm_name == "database_access":
            m = _DATABASE_PERM_RE.match(vm_name or "")
            if m:
                db_ids.add(int(m.group("id")))
        elif perm_name == "datasource_access":
            datasource_perms.add(vm_name)
        elif perm_name == "schema_access":
            schema_perms.add(vm_name)
        elif perm_name == "catalog_access":
            catalog_perms.add(vm_name)
    return False, db_ids, datasource_perms, schema_perms, catalog_perms


def _sync_user_can_access_dataset(
    dataset: Any, user: Any, skip_base_filter: bool = False
) -> bool:
    """Sync replica of ``DatasourceFilter`` access for the Jinja macros.

    1:1 with upstream ``DatasetDAO.find_by_id(dataset_id[, skip_base_filter])``
    → ``get_dataset_access_filters(SqlaTable)``: admins and holders of
    ``all_database_access``/``all_datasource_access`` see all; everyone else
    needs the dataset's database OR its ``perm``/``catalog_perm``/``schema_perm``
    to be granted. Roles include upstream *group* membership — upstream
    ``user_view_menu_names`` (superset_old/security/manager.py:841-880) joins
    ``assoc_user_group``/``assoc_group_role`` so group-granted permissions
    count in every check.

    ``skip_base_filter`` mirrors the upstream argument: ``metric_macro`` passes
    ``skip_base_filter=is_guest`` (embedded/guest access is validated at the
    dashboard level), whereas ``dataset_macro`` never skips the base filter, so
    guests are subject to it like any other user.

    Without this gate ``{{ metric('m', <id>) }}`` / ``{{ dataset(<id>) }}``
    (template processing) would leak metric/column expressions from datasets the
    user cannot access.
    """
    if skip_base_filter:
        return True
    if user is None:
        return False

    roles = getattr(user, "roles", []) or []
    if any(getattr(r, "name", None) == "Admin" for r in roles):
        return True
    role_ids = [r.id for r in roles if getattr(r, "id", None) is not None]

    # Group-inherited roles — 1:1 with the assoc_user_group/assoc_group_role
    # EXISTS terms of upstream ``user_view_menu_names`` and with the upstream
    # base ``get_user_roles`` (user.roles + roles of the user's groups).
    user_id = getattr(user, "id", None)
    if user_id is not None:
        try:
            group_roles = _sync_get_user_group_roles(int(user_id))
        except Exception:  # noqa: BLE001
            logger.debug("Failed to load user group roles", exc_info=True)
            group_roles = []
        if any(name == "Admin" for _, name in group_roles):
            return True
        seen_ids = set(role_ids)
        role_ids.extend(rid for rid, _ in group_roles if rid not in seen_ids)

    if not role_ids:
        return False

    rows = _collect_role_perm_rows(role_ids)
    has_global, db_ids, datasource_perms, schema_perms, catalog_perms = (
        _classify_perm_rows(rows)
    )
    if has_global:
        return True

    return (
        dataset.database_id in db_ids
        or bool(dataset.perm and dataset.perm in datasource_perms)
        or bool(dataset.catalog_perm and dataset.catalog_perm in catalog_perms)
        or bool(dataset.schema_perm and dataset.schema_perm in schema_perms)
    )


def _sync_role_exists(role_name: str) -> bool:
    """Check that a role row exists — sync analogue of the upstream
    get_public_role().

    Uses the same module-level sync engine as ``_sync_get_rls_rules`` since
    Jinja rendering is synchronous.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    engine = _get_sync_engine()
    with Session(engine) as session:
        row = session.execute(
            text("SELECT 1 FROM ab_role WHERE name = :name LIMIT 1"),
            {"name": role_name},
        ).first()
    return row is not None


def _sync_get_user_group_roles(user_id: int) -> list[tuple[int, str]]:
    """Return ``(id, name)`` of roles inherited via upstream group membership.

    Mirrors the group-role term of the upstream base
    ``SecurityManager.get_user_roles``:
    ``[role for group in user.groups for role in group.roles]``.

    Uses the same module-level sync SQLAlchemy engine as ``_sync_get_rls_rules``
    so that it is safe to call from synchronous Jinja template rendering.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    engine = _get_sync_engine()
    stmt = text(
        "SELECT DISTINCT r.id, r.name FROM ab_role r "
        "JOIN ab_group_role gr ON gr.role_id = r.id "
        "JOIN ab_user_group ug ON ug.group_id = gr.group_id "
        "WHERE ug.user_id = :user_id"
    )
    with Session(engine) as session:
        rows = session.execute(stmt, {"user_id": user_id}).all()
    return [(row[0], row[1]) for row in rows if row[1]]


def _sync_get_user_group_role_names(user_id: int) -> list[str]:
    """Return role names inherited via upstream group membership for a user."""
    return [name for _, name in _sync_get_user_group_roles(user_id)]


def _sync_get_rls_rules(table: Any, user: Any) -> list[str]:
    """Synchronously retrieve RLS filter clauses for ``user`` on ``table``.

    Ported 1:1 from the logic in
    ``superset_old/security/manager.py::SupersetSecurityManager.get_rls_filters``
    and ``get_guest_rls_filters``.  Since Jinja template rendering is
    synchronous, we cannot ``await`` the async security manager; instead
    we reproduce the same SQL query directly via a sync session.

    Returns a sorted list of SQL clause strings (same contract as the
    original ``ExtraCache.current_user_rls_rules``).
    """
    from sqlalchemy.orm import Session

    from superset.models.connectors import (
        RLSFilterRoles,
        RLSFilterTables,
        RowLevelSecurityFilter,
    )
    from superset.utils.core import RowLevelSecurityFilterType

    # Guest user path: read RLS rules directly from the guest token attributes.
    # Inline the same check as AsyncSecurityManager.is_guest_user() to avoid
    # needing a manager instance (Jinja rendering is synchronous).
    is_guest = feature_flag_manager.is_feature_enabled("EMBEDDED_SUPERSET") and getattr(
        user, "is_guest", False
    )

    if is_guest:
        rls_rules: list[dict[str, Any]] = getattr(user, "rls_rules", [])
        clauses = [
            rule["clause"]
            for rule in rls_rules
            if rule.get("clause")
            and (not rule.get("dataset") or str(rule.get("dataset")) == str(table.id))
        ]
        return sorted(clauses)

    # Authenticated user path: query the RowLevelSecurityFilter table.
    # Role set mirrors the upstream get_user_roles: direct roles + roles
    # inherited via group membership (superset_old/security/manager.py:2598 →
    # the upstream app-builder security manager).
    try:
        user_role_ids = [r.id for r in getattr(user, "roles", []) or []]
    except Exception:  # noqa: BLE001
        user_role_ids = []
    try:
        user_id = getattr(user, "id", None)
        if user_id is not None:
            direct_ids = set(user_role_ids)
            user_role_ids.extend(
                role_id
                for role_id, _ in _sync_get_user_group_roles(int(user_id))
                if role_id not in direct_ids
            )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to resolve group roles for RLS", exc_info=True)

    if not user_role_ids:
        return []

    try:
        from sqlalchemy import and_, or_

        filter_tables_sq = select(RLSFilterTables.c.rls_filter_id).where(
            RLSFilterTables.c.table_id == table.id
        )
        regular_filter_roles_sq = (
            select(RLSFilterRoles.c.rls_filter_id)
            .join(
                RowLevelSecurityFilter,
                RLSFilterRoles.c.rls_filter_id == RowLevelSecurityFilter.id,
            )
            .where(
                RowLevelSecurityFilter.filter_type == RowLevelSecurityFilterType.REGULAR
            )
            .where(RLSFilterRoles.c.role_id.in_(user_role_ids))
        )
        base_filter_roles_sq = (
            select(RLSFilterRoles.c.rls_filter_id)
            .join(
                RowLevelSecurityFilter,
                RLSFilterRoles.c.rls_filter_id == RowLevelSecurityFilter.id,
            )
            .where(
                RowLevelSecurityFilter.filter_type == RowLevelSecurityFilterType.BASE
            )
            .where(RLSFilterRoles.c.role_id.in_(user_role_ids))
        )
        stmt = (
            select(RowLevelSecurityFilter.clause)
            .where(RowLevelSecurityFilter.id.in_(filter_tables_sq))
            .where(
                or_(
                    and_(
                        RowLevelSecurityFilter.filter_type
                        == RowLevelSecurityFilterType.REGULAR,
                        RowLevelSecurityFilter.id.in_(regular_filter_roles_sq),
                    ),
                    and_(
                        RowLevelSecurityFilter.filter_type
                        == RowLevelSecurityFilterType.BASE,
                        RowLevelSecurityFilter.id.notin_(base_filter_roles_sq),
                    ),
                )
            )
        )
        engine = _get_sync_engine()
        with Session(engine) as session:
            rows = session.execute(stmt).scalars().all()
        return sorted(str(c) for c in rows if c)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to retrieve RLS rules for user in Jinja context")
        return []


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
    # 1:1 with upstream ``DatasetDAO.find_by_id(dataset_id)`` (no
    # ``skip_base_filter``): the ``DatasourceFilter`` RBAC base filter applies
    # to every user, including guests. A denial manifests as
    # ``DatasetNotFoundError`` exactly like the original's filtered lookup
    # returning ``None`` — without it, ``{{ dataset(<id>) }}`` would leak the
    # dataset's column/metric SQL expressions to a user with no access.
    dataset = _sync_find_dataset(dataset_id)
    if not dataset or not _sync_user_can_access_dataset(dataset, get_current_user()):
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


def _loads_request_json(data: str | None) -> dict[str, Any]:
    """JSON-decode a string, returning ``{}`` on failure.

    1:1 with ``superset_old/views/utils.py::loads_request_json``.
    """
    if not data:
        return {}
    try:
        return json.loads(data)
    except (TypeError, json.JSONDecodeError):
        return {}


def _merge_payload_into_form_data(
    payload: dict[str, Any],
    form_data: dict[str, Any],
) -> int | None:
    """Process the request payload and merge into *form_data* in-place.

    Returns the dataset ID if an early-return path (``datasource.id``) is
    found, otherwise ``None``.  Mirrors original lines 1042-1046.
    """
    # Early return: top-level ``datasource.id`` in JSON body
    # (chart-data API path — original line 1042-1043).
    # Guard with isinstance: in task contexts payload may come from
    # Slice.form_data where "datasource" is a string like "5__table",
    # not a dict — calling .get("id") on a string raises AttributeError.
    _datasource_val = payload.get("datasource")
    if isinstance(_datasource_val, dict):
        if dataset_id := _datasource_val.get("id"):
            return dataset_id

    # Merge nested ``form_data`` from the JSON body (original line 1044).
    nested_form_data = payload.get("form_data")
    if isinstance(nested_form_data, dict):
        form_data.update(nested_form_data)
    elif isinstance(nested_form_data, str):
        # form-encoded body: middleware yields {"form_data": "<json>"}
        form_data.update(_loads_request_json(nested_form_data))
    return None


def _merge_query_string_into_form_data(form_data: dict[str, Any]) -> None:
    """Merge ``form_data`` from query-string into *form_data* in-place.

    Mirrors original lines 1047-1048 (``request.args.get("form_data")``).
    """
    _request = get_current_request()
    if _request is None:
        return
    _query_params = getattr(_request, "query_params", None)
    if _query_params is None:
        return
    args_form_data = _query_params.get("form_data")
    if args_form_data:
        form_data.update(_loads_request_json(args_form_data))


def _dataset_id_from_chart(chart_id: Any, exc_message: str) -> int:
    """Look up a chart by ID and return its datasource_id synchronously.

    Mirrors ``ChartDAO.find_by_id`` (superset_old/daos/base.py lines 68-72):
    ``ValueError`` from ``int(chart_id)`` for a non-numeric *chart_id*, or
    ``StatementError`` from the DB for an invalid parameter type, are both caught
    so that the caller raises ``SupersetTemplateException`` (400-level) instead
    of propagating a 500.
    """
    from sqlalchemy.orm import Session

    from superset.models.slice import Slice

    engine = _get_sync_engine()
    try:
        chart_id_int = int(chart_id)
    except (ValueError, TypeError):
        raise SupersetTemplateException(exc_message) from None
    try:
        with Session(engine) as session:
            chart = (
                session.execute(select(Slice).where(Slice.id == chart_id_int))
                .scalars()
                .one_or_none()
            )
    except StatementError:
        raise SupersetTemplateException(exc_message) from None
    if not chart:
        raise SupersetTemplateException(exc_message)
    return chart.datasource_id  # type: ignore[return-value]


def get_dataset_id_from_context(metric_key: str) -> int:
    """
    Retrieves the Dataset ID from the template context.

    1:1 with ``superset_old/jinja_context.py::get_dataset_id_from_context``.
    The original reads the request JSON body, ``request.form``,
    ``request.args`` and ``g.form_data``.  In Liteset the request-scoped
    ``_form_data_ctx`` ContextVar (set by the ``request_context``
    middleware to the full JSON body) is the primary source.  We also
    check ``request.query_params["form_data"]`` (Litestar equivalent of
    ``request.args.get("form_data")``) and merge a nested ``form_data``
    dict from the payload, exactly like the original does.  The Celery
    task path (``get_task_form_data``) mirrors ``g.form_data``.
    """
    from superset.utils.core import get_form_data as get_task_form_data

    exc_message = (
        f"Please specify the Dataset ID for the ``{metric_key}`` "
        f"metric in the Jinja macro."
    )

    # --- Build merged form_data, mirroring the original ----------------
    # Original lines 1034-1048:
    #   form_data = {}
    #   if has_request_context():
    #       payload = request.get_json(...)
    #       if payload.datasource.id -> early return
    #       form_data.update(payload.get("form_data", {}))
    #       form_data.update(loads_request_json(request.form.get("form_data")))
    #       form_data.update(loads_request_json(request.args.get("form_data")))
    #   form_data = form_data or g.form_data
    form_data: dict[str, Any] = {}

    # The middleware sets _form_data_ctx to the full JSON body (or the
    # form-url-encoded key/value dict).  This mirrors ``payload``.
    payload = get_form_data()

    if payload:
        early_id = _merge_payload_into_form_data(payload, form_data)
        if early_id is not None:
            return early_id

    _merge_query_string_into_form_data(form_data)

    # Original line 1050: ``form_data = form_data or g.form_data``
    # In the original, ``g.form_data`` is the chart's flat form_data dict
    # set by warm_up_cache (e.g. ``{'datasource': '5__table', ...}``).
    # In liteset, ``_form_data_ctx`` serves BOTH as the HTTP request body
    # envelope AND as the flat form_data from warm_up_cache.
    # ``_merge_payload_into_form_data`` handles the HTTP envelope case but
    # leaves ``form_data`` empty when ``payload`` IS already the flat dict
    # (because it looks for nested ``payload["form_data"]`` or a dict-typed
    # ``payload["datasource"]`` -- neither present in the flat case).
    # So if ``form_data`` is still empty after the merge, fall back to using
    # ``payload`` directly (mirrors ``g.form_data`` from the original).
    if not form_data:
        form_data = payload or get_task_form_data()

    if not form_data:
        raise SupersetTemplateException(exc_message)

    # Check datasource field (can be dict or "id__type" string)
    if datasource_info := form_data.get("datasource"):
        if isinstance(datasource_info, dict):
            return datasource_info["id"]
        return datasource_info.split("__")[0]

    url_params = form_data.get("queries", [{}])[0].get("url_params", {})
    if dataset_id := url_params.get("datasource_id"):
        return dataset_id
    if chart_id := (form_data.get("slice_id") or url_params.get("slice_id")):
        # Synchronous fallback for chart lookup — use cached engine.
        return _dataset_id_from_chart(chart_id, exc_message)

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

    # 1:1 with upstream
    # ``DatasetDAO.find_by_id(dataset_id, skip_base_filter=is_guest)``: load
    # the dataset, then enforce the ``DatasourceFilter`` RBAC for non-guest
    # users. A denial manifests as ``DatasetNotFoundError`` exactly like the
    # original's filtered lookup returning ``None`` — without it,
    # ``{{ metric('m', <id>) }}`` would leak metric expressions from datasets
    # the caller cannot access.
    user = get_current_user()
    # Inline the same check as AsyncSecurityManager.is_guest_user() to avoid
    # needing a manager instance (Jinja rendering is synchronous).
    is_guest = feature_flag_manager.is_feature_enabled("EMBEDDED_SUPERSET") and getattr(
        user, "is_guest", False
    )
    dataset = _sync_find_dataset(dataset_id)
    if not dataset or not _sync_user_can_access_dataset(
        dataset, user, skip_base_filter=is_guest
    ):
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
