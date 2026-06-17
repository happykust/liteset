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
"""Base database engine spec with shared functionality for all database backends."""

from __future__ import annotations

import functools
import json as _json
import logging
import re
import warnings
from datetime import datetime
from re import Match, Pattern
from typing import (
    Any,
    Callable,
    ContextManager,
    NamedTuple,
    TYPE_CHECKING,
    TypedDict,
    Union,
)

from sqlalchemy import column, select, types
from sqlalchemy.engine.interfaces import Compiled, Dialect
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import literal_column, quoted_name, text
from sqlalchemy.sql.expression import ColumnClause, Select, TextClause
from sqlalchemy.sql.type_api import TypeEngine

from superset.constants import TimeGrain as TimeGrainConstants
from superset.databases.utils import make_url_safe
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.i18n import gettext as _
from superset.sql.parse import LimitMethod, RLSMethod, SQLScript, SQLStatement, Table
from superset.utils.hashing import md5_sha_from_str
from superset.utils.json import redact_sensitive, reveal_sensitive
from superset.utils.network import is_hostname_valid, is_port_open

if TYPE_CHECKING:
    from sqlalchemy.engine.base import Engine
    from sqlalchemy.engine.reflection import Inspector

    from superset.models.core import Database
    from superset.models.sql_lab import Query

logger = logging.getLogger(__name__)

# Alias used in broad exception catches across the engine spec layer.
# We support 50+ drivers so catching the generic Exception is unavoidable;
# the alias makes it intentional rather than accidental.
GenericDBException = Exception


ColumnTypeMapping = tuple[
    Pattern[str],
    Union[TypeEngine, Callable[[Match[str]], TypeEngine]],
    "GenericDataType",
]


class ColumnSpec(NamedTuple):
    sqla_type: TypeEngine | str
    generic_type: "GenericDataType"
    is_dttm: bool
    python_date_format: str | None = None


class MetricType(TypedDict, total=False):
    metric_name: str
    expression: str
    verbose_name: str | None
    metric_type: str | None
    description: str | None
    d3format: str | None
    currency: str | None
    warning_text: str | None
    extra: str | None


class TimeGrain(NamedTuple):
    name: str  # TODO: remove redundant field
    label: str
    function: str
    duration: str | None


builtin_time_grains: dict[str | None, str] = {
    TimeGrainConstants.SECOND: _("Second"),
    TimeGrainConstants.FIVE_SECONDS: _("5 second"),
    TimeGrainConstants.THIRTY_SECONDS: _("30 second"),
    TimeGrainConstants.MINUTE: _("Minute"),
    TimeGrainConstants.FIVE_MINUTES: _("5 minute"),
    TimeGrainConstants.TEN_MINUTES: _("10 minute"),
    TimeGrainConstants.FIFTEEN_MINUTES: _("15 minute"),
    TimeGrainConstants.THIRTY_MINUTES: _("30 minute"),
    TimeGrainConstants.HOUR: _("Hour"),
    TimeGrainConstants.SIX_HOURS: _("6 hour"),
    TimeGrainConstants.DAY: _("Day"),
    TimeGrainConstants.WEEK: _("Week"),
    TimeGrainConstants.MONTH: _("Month"),
    TimeGrainConstants.QUARTER: _("Quarter"),
    TimeGrainConstants.YEAR: _("Year"),
    TimeGrainConstants.WEEK_STARTING_SUNDAY: _("Week starting Sunday"),
    TimeGrainConstants.WEEK_STARTING_MONDAY: _("Week starting Monday"),
    TimeGrainConstants.WEEK_ENDING_SATURDAY: _("Week ending Saturday"),
    TimeGrainConstants.WEEK_ENDING_SUNDAY: _("Week ending Sunday"),
}


@functools.lru_cache(maxsize=1)
def _time_grain_config() -> tuple[
    dict[str, dict[str, str]], tuple[str, ...], dict[str, str]
]:
    """Cached ``(TIME_GRAIN_ADDON_EXPRESSIONS, TIME_GRAIN_DENYLIST, TIME_GRAIN_ADDONS)``
    from config.

    Mirrors the ``app.config[...]`` reads in the original
    ``get_time_grain_expressions`` and ``get_time_grains``. These are static at
    runtime, so the result is cached to avoid rebuilding ``SupersetSettings`` on
    every call (this runs per chart query). Falls back to the upstream defaults
    (``{}`` / ``()`` / ``{}``) when settings can't be loaded.
    """
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        return (
            dict(getattr(settings, "time_grain_addon_expressions", {}) or {}),
            tuple(getattr(settings, "time_grain_denylist", []) or []),
            dict(getattr(settings, "time_grain_addons", {}) or {}),
        )
    except Exception:  # noqa: BLE001
        return {}, (), {}


class TimestampExpression(ColumnClause):  # type: ignore[type-arg]
    def __init__(self, expr: str, col: ColumnClause, **kwargs: Any) -> None:
        """Renders native column elements inside a string SQL expression.

        :param expr: SQL expression with ``{col}`` placeholders.
        :param col: The target column (rendered with engine-specific quoting).
        """
        super().__init__(expr, **kwargs)
        self.col = col

    @property
    def _constructor(self) -> type[ColumnClause]:
        # Ensures the column label renders correctly when proxied to the outer
        # query. See https://github.com/sqlalchemy/sqlalchemy/issues/4730
        return ColumnClause


@compiles(TimestampExpression)
def compile_timegrain_expression(
    element: TimestampExpression, compiler: Compiled, **kwargs: Any
) -> str:
    return element.name.replace("{col}", compiler.process(element.col, **kwargs))


class ResultSetColumnType(TypedDict, total=False):
    column_name: str
    name: str
    type: Any
    type_generic: Any
    is_dttm: bool | None
    nullable: bool
    default: Any
    autoincrement: str
    comment: str | None


SQLAColumnType = TypedDict(
    "SQLAColumnType",
    {
        "name": str,
        "type": Any,
        "nullable": bool,
        "default": Any,
        "autoincrement": str,
        "comment": str | None,
    },
    total=False,
)


def convert_inspector_columns(
    cols: list[SQLAColumnType],
) -> list[ResultSetColumnType]:
    result_set_columns: list[ResultSetColumnType] = []
    for col in cols:
        result_set_columns.append({"column_name": col.get("name"), **col})  # type: ignore[typeddict-item]
    return result_set_columns


from superset.typing import GenericDataType  # noqa: E402


class BaseEngineSpec:  # noqa: PLR0904
    """Abstract class for database engine specific configurations.

    Attributes:
        allows_alias_to_source_column: Whether the engine is able to pick the
                                       source column for aggregation clauses
                                       used in ORDER BY when a column in SELECT
                                       has an alias that is the same as a source
                                       column.
        allows_hidden_orderby_agg:     Whether the engine allows ORDER BY to
                                       directly use aggregation clauses, without
                                       having to add the same aggregation in SELECT.
    """

    engine_name: str | None = None
    engine = "base"
    engine_aliases: set[str] = set()
    drivers: dict[str, str] = {}
    default_driver: str | None = None

    sqlalchemy_uri_placeholder = (
        "engine+driver://user:password@host:port/dbname[?key=value&key=value...]"
    )

    disable_ssh_tunneling = False

    _date_trunc_functions: dict[str, str] = {}
    _time_grain_expressions: dict[str | None, str] = {}
    _default_column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r"^string", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^n((var)?char|text)", re.IGNORECASE),
            types.UnicodeText(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^(var)?char", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^(tiny|medium|long)?text", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^smallint", re.IGNORECASE),
            types.SmallInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^int(eger)?", re.IGNORECASE),
            types.Integer(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bigint", re.IGNORECASE),
            types.BigInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^long", re.IGNORECASE),
            types.Float(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^decimal", re.IGNORECASE),
            types.Numeric(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^numeric", re.IGNORECASE),
            types.Numeric(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^float", re.IGNORECASE),
            types.Float(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^double", re.IGNORECASE),
            types.Float(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^real", re.IGNORECASE),
            types.REAL,
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^smallserial", re.IGNORECASE),
            types.SmallInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^serial", re.IGNORECASE),
            types.Integer(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bigserial", re.IGNORECASE),
            types.BigInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^money", re.IGNORECASE),
            types.Numeric(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^timestamp", re.IGNORECASE),
            types.TIMESTAMP(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^datetime", re.IGNORECASE),
            types.DateTime(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^date", re.IGNORECASE),
            types.Date(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^time", re.IGNORECASE),
            types.Time(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^interval", re.IGNORECASE),
            types.Interval(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^bool(ean)?", re.IGNORECASE),
            types.Boolean(),
            GenericDataType.BOOLEAN,
        ),
    )

    # Engine-specific type mappings to check prior to the defaults.
    column_type_mappings: tuple[ColumnTypeMapping, ...] = ()
    column_type_mutators: dict[TypeEngine, Callable[[Any], Any]] = {}

    time_groupby_inline = False
    limit_method = LimitMethod.FORCE_LIMIT
    supports_multivalues_insert = False
    allows_joins = True
    allows_subqueries = True
    allows_alias_in_select = True
    allows_alias_in_orderby = True
    allows_sql_comments = True
    allows_escaped_colons = True

    allows_alias_to_source_column = True
    allows_hidden_orderby_agg = True
    allows_hidden_cc_in_orderby = False
    allows_cte_in_subquery = True
    cte_alias = "__cte"

    # Disallowed connection query parameters by driver name
    disallow_uri_query_params: dict[str, set[str]] = {}
    use_equality_for_boolean_filters = False
    # Query parameters that will always be used on every connection by driver name
    enforce_uri_query_params: dict[str, dict[str, Any]] = {}

    force_column_alias_quotes = False
    arraysize = 0
    max_column_name_length: int | None = None
    try_remove_schema_from_table_name = True
    run_multiple_statements_as_one = False

    custom_errors: dict[Pattern[str], tuple[str, Any, dict[str, Any]]] = {}

    # List of JSON path to fields in ``encrypted_extra`` that should be masked.
    encrypted_extra_sensitive_fields: set[str] = {"$.*"}
    supports_file_upload = True
    supports_dynamic_schema = False
    supports_catalog = False
    supports_dynamic_catalog = False
    supports_cross_catalog_queries = False
    supports_oauth2 = False
    oauth2_scope: str = ""
    oauth2_authorization_request_uri: str | None = None
    oauth2_token_request_uri: str | None = None
    # "data" or "json" — Keycloak and a few other IDPs reject json bodies.
    oauth2_token_request_type: str = "data"  # noqa: S105
    # Driver-specific exception that should trigger the OAuth2 dance.
    # Per-engine specs override this with a concrete exception class
    # (e.g. ``TrinoAuthError``).  Defaults to a sentinel that never matches.
    oauth2_exception: type[BaseException] = type(
        "_NoOAuth2Exception", (BaseException,), {}
    )

    has_query_id_before_execute = True

    @classmethod
    def is_oauth2_enabled(cls) -> bool:
        """Return True if the engine has a configured OAuth2 client."""
        from superset.utils.oauth2 import get_oauth2_clients

        return cls.supports_oauth2 and cls.engine_name in get_oauth2_clients()

    @classmethod
    def get_oauth2_config(cls) -> "Any | None":
        """Return OAuth2 client config dict, or ``None`` if not registered."""
        from superset.utils.oauth2 import (
            get_oauth2_clients,
            validate_oauth2_client_config,
        )

        clients = get_oauth2_clients()
        if cls.engine_name not in clients:
            return None

        client = clients[cls.engine_name]
        # Apply engine-spec defaults for the optional fields, then validate.
        merged: dict[str, Any] = {
            "id": client.get("id"),
            "secret": client.get("secret"),
            "scope": client.get("scope") or cls.oauth2_scope,
            "authorization_request_uri": client.get(
                "authorization_request_uri",
                cls.oauth2_authorization_request_uri,
            ),
            "token_request_uri": client.get(
                "token_request_uri", cls.oauth2_token_request_uri
            ),
            "request_content_type": client.get(
                "request_content_type", cls.oauth2_token_request_type
            ),
        }
        if "redirect_uri" in client:
            merged["redirect_uri"] = client["redirect_uri"]
        # Drop keys with None values so the schema's ``required`` checks
        # surface the missing fields rather than masking them as type errors.
        cleaned = {k: v for k, v in merged.items() if v is not None}
        return validate_oauth2_client_config(cleaned)

    @classmethod
    def get_oauth2_authorization_uri(
        cls,
        config: dict[str, Any],
        state: dict[str, Any],
    ) -> str:
        from urllib.parse import urlencode, urljoin

        from superset.utils.oauth2 import encode_oauth2_state

        params = {
            "scope": config["scope"],
            "access_type": "offline",
            "include_granted_scopes": "false",
            "response_type": "code",
            "state": encode_oauth2_state(state),
            "redirect_uri": config["redirect_uri"],
            "client_id": config["id"],
            "prompt": "consent",
        }
        return urljoin(config["authorization_request_uri"], "?" + urlencode(params))

    @classmethod
    async def get_oauth2_token(
        cls,
        config: dict[str, Any],
        code: str,
    ) -> dict[str, Any]:
        """Exchange an authorization code for refresh/access tokens
        (async, uses httpx)."""
        import httpx

        from superset.utils.oauth2 import get_oauth2_timeout

        timeout = get_oauth2_timeout().total_seconds()
        body = {
            "code": code,
            "client_id": config["id"],
            "client_secret": config["secret"],
            "redirect_uri": config["redirect_uri"],
            "grant_type": "authorization_code",
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            if config["request_content_type"] == "data":
                response = await client.post(config["token_request_uri"], data=body)
            else:
                response = await client.post(config["token_request_uri"], json=body)
        return response.json()

    @classmethod
    async def get_oauth2_fresh_token(
        cls,
        config: dict[str, Any],
        refresh_token: str,
    ) -> dict[str, Any]:
        import httpx

        from superset.utils.oauth2 import get_oauth2_timeout

        timeout = get_oauth2_timeout().total_seconds()
        body = {
            "client_id": config["id"],
            "client_secret": config["secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            if config["request_content_type"] == "data":
                response = await client.post(config["token_request_uri"], data=body)
            else:
                response = await client.post(config["token_request_uri"], json=body)
        return response.json()

    @classmethod
    def get_oauth2_fresh_token_sync(
        cls,
        config: dict[str, Any],
        refresh_token: str,
    ) -> dict[str, Any]:
        """Sync sibling of :meth:`get_oauth2_fresh_token`.

        Used from the psycopg2 impersonation path; uses ``httpx.Client``
        (not ``requests`` which is not installed).
        """
        import httpx

        from superset.utils.oauth2 import get_oauth2_timeout

        timeout = get_oauth2_timeout().total_seconds()
        body = {
            "client_id": config["id"],
            "client_secret": config["secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        with httpx.Client(timeout=timeout) as client:
            if config["request_content_type"] == "data":
                response = client.post(config["token_request_uri"], data=body)
            else:
                response = client.post(config["token_request_uri"], json=body)
        return response.json()

    @classmethod
    def needs_oauth2(cls, ex: Exception) -> bool:
        """Return True if *ex* is the engine's OAuth2 exception.

        The current-user guard prevents Celery workers (which have no request
        user) from triggering an OAuth2 dance that can't be completed.
        """
        from superset.utils.core import get_current_user

        return get_current_user() is not None and isinstance(ex, cls.oauth2_exception)

    @classmethod
    async def start_oauth2_dance(
        cls,
        database: "Database",
        user_id: int | None = None,
        default_redirect_uri: str | None = None,
    ) -> None:
        """Raise :class:`OAuth2RedirectError` to trigger the browser OAuth2 flow.

        User identity comes from the request-scoped ContextVar (set by
        AuthMiddleware).  The redirect URI defaults to
        ``WEBDRIVER_BASEURL`` / ``DATABASE_OAUTH2_REDIRECT_URI`` but can be
        supplied explicitly by callers (e.g. ``DatabaseTestConnectionCommand``).
        """
        from uuid import uuid4

        from superset.exceptions import OAuth2Error, OAuth2RedirectError
        from superset.utils.core import get_current_user
        from superset.utils.oauth2 import get_default_oauth2_redirect_uri

        if user_id is None:
            user = get_current_user()
            user_id = getattr(user, "id", None) if user is not None else None
            if user_id is None:
                raise OAuth2Error(
                    "No authenticated user in context — cannot start OAuth2 dance"
                )

        if default_redirect_uri is None:
            default_redirect_uri = get_default_oauth2_redirect_uri()

        tab_id = str(uuid4())
        state: dict[str, Any] = {
            "database_id": database.id,
            "user_id": user_id,
            "default_redirect_uri": default_redirect_uri,
            "tab_id": tab_id,
        }
        config = database.get_oauth2_config()
        if config is None:
            raise OAuth2Error("No configuration found for OAuth2")
        url = cls.get_oauth2_authorization_uri(config, state)
        raise OAuth2RedirectError(url, tab_id, default_redirect_uri)

    @classmethod
    def get_rls_method(cls) -> RLSMethod:
        """Return AS_SUBQUERY (safer) or AS_PREDICATE based on dialect capabilities."""
        return (
            RLSMethod.AS_SUBQUERY
            if cls.allows_subqueries and cls.allows_alias_in_select
            else RLSMethod.AS_PREDICATE
        )

    @classmethod
    def supports_url(cls, url: URL) -> bool:
        backend = url.get_backend_name()
        driver = url.get_driver_name()
        return cls.supports_backend(backend, driver)

    @classmethod
    def supports_backend(cls, backend: str, driver: str | None = None) -> bool:
        if backend != cls.engine and backend not in cls.engine_aliases:
            return False
        if not cls.drivers or driver is None:
            return True
        return driver in cls.drivers

    @classmethod
    def get_impersonation_key(cls, user: Any | None) -> Any:
        """Return username for per-user query cache key, or ``None`` for anonymous."""
        return user.username if user else None

    @classmethod
    def get_default_catalog(
        cls,
        database: Database,
    ) -> str | None:
        return None

    @classmethod
    def get_default_schema(cls, database: Database, catalog: str | None) -> str | None:
        with database.get_inspector(catalog=catalog) as inspector:
            return inspector.default_schema_name

    @classmethod
    def get_schema_from_engine_params(
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> str | None:
        return None

    @classmethod
    def get_default_schema_for_query(
        cls,
        database: Database,
        query: Any,
        template_params: dict[str, Any] | None = None,
    ) -> str | None:
        """Return the schema used for unqualified table references in a query.

        Priority: dynamic (per-query) schema → URI/connect_args schema → DB default.
        """
        if cls.supports_dynamic_schema:
            return getattr(query, "schema", None)

        try:
            connect_args = database.get_extra()["engine_params"]["connect_args"]
        except KeyError:
            connect_args = {}
        sqlalchemy_uri = make_url_safe(database.sqlalchemy_uri)
        if schema := cls.get_schema_from_engine_params(sqlalchemy_uri, connect_args):
            return schema

        return cls.get_default_schema(database, getattr(query, "catalog", None))

    @classmethod
    def get_allows_alias_in_select(
        cls,
        database: Database,
    ) -> bool:
        return cls.allows_alias_in_select

    @classmethod
    def get_dbapi_exception_mapping(cls) -> dict[type[Exception], type[Exception]]:
        return {}

    @classmethod
    def parse_error_exception(cls, exception: Exception) -> Exception:
        return exception

    @classmethod
    def get_dbapi_mapped_exception(cls, exception: Exception) -> Exception:
        new_exception = cls.get_dbapi_exception_mapping().get(type(exception))
        if not new_exception:
            return cls.parse_error_exception(exception)
        return new_exception(str(exception))

    @classmethod
    def impersonate_user(
        cls,
        database: Database,
        username: str | None,
        user_token: str | None,
        url: URL,
        engine_kwargs: dict[str, Any],
    ) -> tuple[URL, dict[str, Any]]:
        from inspect import signature

        url = cls.get_url_for_impersonation(url, True, username, user_token)

        connect_args = engine_kwargs.setdefault("connect_args", {})
        args: list[Any] = [connect_args, url, username, user_token]
        if "database" in signature(cls.update_impersonation_config).parameters:
            args.insert(0, database)

        cls.update_impersonation_config(*args)

        return url, engine_kwargs

    @classmethod
    def get_url_for_impersonation(
        cls,
        url: URL,
        impersonate_user: bool,
        username: str | None,
        access_token: str | None,  # noqa: ARG003
    ) -> URL:
        if impersonate_user and username is not None:
            url = url.set(username=username)
        return url

    @classmethod
    def update_impersonation_config(
        cls,
        database: Database,
        connect_args: dict[str, Any],
        uri: str,
        username: str | None,
        access_token: str | None,
    ) -> None:
        """No-op base; engines like Hive/Presto override to set connect_args."""

    @classmethod
    def get_allow_cost_estimate(
        cls,
        extra: dict[str, Any],
    ) -> bool:
        return False

    @classmethod
    def get_text_clause(cls, clause: str) -> TextClause:
        if cls.allows_escaped_colons:
            clause = clause.replace(":", "\\:")
        return text(clause)

    @classmethod
    def get_engine(
        cls,
        database: Database,
        catalog: str | None = None,
        schema: str | None = None,
        source: Any | None = None,
    ) -> ContextManager[Engine]:
        """
        Return an engine context manager.

            >>> with DBEngineSpec.get_engine(database, catalog, schema, source) as engine:
            ...     connection = engine.connect()
            ...     connection.execute(sql)

        """  # noqa: E501
        return database.get_sqla_engine(catalog=catalog, schema=schema, source=source)

    @classmethod
    def get_timestamp_expr(
        cls,
        col: ColumnClause,
        pdf: str | None,
        time_grain: str | None,
    ) -> TimestampExpression:
        if time_grain:
            type_ = str(getattr(col, "type", ""))
            time_expr = cls.get_time_grain_expressions().get(time_grain)
            if not time_expr:
                raise NotImplementedError(
                    f"No grain spec for {time_grain} for database {cls.engine}"
                )
            if type_ and "{func}" in time_expr:
                date_trunc_function = cls._date_trunc_functions.get(type_)
                if date_trunc_function:
                    time_expr = time_expr.replace("{func}", date_trunc_function)
            if type_ and "{type}" in time_expr:
                date_trunc_function = cls._date_trunc_functions.get(type_)
                if date_trunc_function:
                    time_expr = time_expr.replace("{type}", type_)
        else:
            time_expr = "{col}"

        if pdf == "epoch_s":
            time_expr = time_expr.replace("{col}", cls.epoch_to_dttm())
        elif pdf == "epoch_ms":
            time_expr = time_expr.replace("{col}", cls.epoch_ms_to_dttm())

        return TimestampExpression(time_expr, col, type_=col.type)

    @classmethod
    def _sort_time_grains(
        cls, val: tuple[str | None, str], index: int
    ) -> float | int | str:
        pos = {
            "FIRST": 0,
            "SECOND": 1,
            "THIRD": 2,
            "LAST": 3,
        }

        if val[0] is None:
            return pos["FIRST"]

        prog = re.compile(r"(.*\/)?(P|PT)([0-9\.]+)(S|M|H|D|W|M|Y)(\/.*)?")
        result = prog.match(val[0])

        if result is None:
            return pos["LAST"]

        second_minute_hour = ["S", "M", "H"]
        day_week_month_year = ["D", "W", "M", "Y"]
        is_less_than_day = result.group(2) == "PT"
        interval = result.group(4)
        epoch_time_start_string = result.group(1) or result.group(5)
        has_starting_or_ending = bool(len(epoch_time_start_string or ""))

        def sort_day_week() -> int:
            if has_starting_or_ending:
                return pos["LAST"]
            if is_less_than_day:
                return pos["SECOND"]
            return pos["THIRD"]

        def sort_interval() -> float:
            if is_less_than_day:
                return second_minute_hour.index(interval)
            return day_week_month_year.index(interval)

        plist = {
            0: sort_day_week(),
            1: pos["SECOND"] if is_less_than_day else pos["THIRD"],
            2: sort_interval(),
            3: float(result.group(3)),
        }

        return plist.get(index, 0)

    @classmethod
    def get_time_grain_expressions(cls) -> dict[str | None, str]:
        """Return all enabled time grain expressions (addon merged,
        denylist applied)."""
        time_grain_expressions = cls._time_grain_expressions.copy()
        addon_expressions, denylist, _ = _time_grain_config()
        time_grain_expressions.update(addon_expressions.get(cls.engine, {}))
        for key in denylist:
            time_grain_expressions.pop(key, None)

        return dict(
            sorted(
                time_grain_expressions.items(),
                key=lambda x: (
                    cls._sort_time_grains(x, 0),
                    cls._sort_time_grains(x, 1),
                    cls._sort_time_grains(x, 2),
                    cls._sort_time_grains(x, 3),
                ),
            )
        )

    @classmethod
    def get_time_grains(cls) -> tuple[TimeGrain, ...]:
        ret_list = []
        time_grains = builtin_time_grains.copy()
        # NB: do not unpack into ``_`` here — that name is the module-level
        # gettext alias used a few lines below as ``_(name)``.
        _addon_expressions, _denylist, time_grain_addons = _time_grain_config()
        time_grains.update(time_grain_addons)
        for duration, func in cls.get_time_grain_expressions().items():
            if duration in time_grains:
                name = time_grains[duration]
                ret_list.append(TimeGrain(name, _(name), func, duration))
        return tuple(ret_list)

    @classmethod
    def epoch_to_dttm(cls) -> str:
        """SQL expression converting epoch-seconds to datetime;
        use ``{col}`` as placeholder."""
        raise NotImplementedError()

    @classmethod
    def epoch_ms_to_dttm(cls) -> str:
        return cls.epoch_to_dttm().replace("{col}", "({col}/1000)")

    @classmethod
    def fetch_data(cls, cursor: Any, limit: int | None = None) -> list[tuple[Any, ...]]:
        if cls.arraysize:
            cursor.arraysize = cls.arraysize
        try:
            if cls.limit_method == LimitMethod.FETCH_MANY and limit:
                return cursor.fetchmany(limit)
            data = cursor.fetchall()
            description = cursor.description or []
            column_mutators = {
                row[0]: func
                for row in description
                if (
                    func := cls.column_type_mutators.get(
                        type(cls.get_sqla_column_type(cls.get_datatype(row[1])))
                    )
                )
            }
            if column_mutators:
                indexes = {row[0]: idx for idx, row in enumerate(description)}
                for row_idx, row in enumerate(data):
                    new_row = list(row)
                    for col_name, func in column_mutators.items():
                        col_idx = indexes[col_name]
                        new_row[col_idx] = func(row[col_idx])
                    data[row_idx] = tuple(new_row)

            return data
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex

    @classmethod
    def expand_data(
        cls,
        columns: list[ResultSetColumnType],
        data: list[dict[Any, Any]],
    ) -> tuple[
        list[ResultSetColumnType], list[dict[Any, Any]], list[ResultSetColumnType]
    ]:
        """Expand nested fields; see Presto spec for an override."""
        return columns, data, []

    @classmethod
    def execute(
        cls,
        cursor: Any,
        query: str,
        database: Database,
        **kwargs: Any,
    ) -> None:
        if cls.arraysize:
            cursor.arraysize = cls.arraysize
        try:
            cursor.execute(query)
        except Exception as ex:
            # start_oauth2_dance is declared async but does no real await — it only
            # assembles the redirect URL and raises OAuth2RedirectError.  execute()
            # runs in a sync thread, so drive the coroutine one step via send(None).
            if database.is_oauth2_enabled() and cls.needs_oauth2(ex):
                dance = cls.start_oauth2_dance(database)
                if hasattr(dance, "send"):  # coroutine — drive it synchronously
                    try:
                        dance.send(None)
                    except StopIteration:
                        pass
            raise cls.get_dbapi_mapped_exception(ex) from ex

    @classmethod
    def handle_cursor(cls, cursor: Any, query: Query) -> None:
        """Hook called between execute and fetchall; override to track progress."""

    @classmethod
    def execute_with_cursor(
        cls,
        cursor: Any,
        sql: str,
        query: Query,
    ) -> None:
        """Execute *sql* then call ``handle_cursor``; some engines (Trino) override."""
        from sqlalchemy.orm import object_session

        from superset.constants import QUERY_CANCEL_KEY

        logger.debug("Query %d: Running query: %s", query.id, sql)
        cls.execute(cursor, sql, query.database, async_=True)
        if not cls.has_query_id_before_execute:
            cancel_query_id = query.database.db_engine_spec.get_cancel_query_id(
                cursor, query
            )
            if cancel_query_id is not None:
                query.set_extra_json_key(QUERY_CANCEL_KEY, cancel_query_id)
                session = object_session(query)
                if session is not None:
                    session.commit()
        logger.debug("Query %d: Handling cursor", query.id)
        cls.handle_cursor(cursor, query)

    @classmethod
    def get_datatype(cls, type_code: Any) -> str | None:
        if isinstance(type_code, str) and type_code != "":
            return type_code.upper()
        return None

    @classmethod
    def get_column_types(
        cls,
        column_type: str | None,
    ) -> tuple[TypeEngine, GenericDataType] | None:
        if not column_type:
            return None

        for regex, sqla_type, generic_type in (
            cls.column_type_mappings + cls._default_column_type_mappings
        ):
            match = regex.match(column_type)
            if not match:
                continue
            if callable(sqla_type):
                return sqla_type(match), generic_type
            return sqla_type, generic_type
        return None

    @classmethod
    def get_column_spec(
        cls,
        native_type: str | None,
        db_extra: dict[str, Any] | None = None,
        source: Any = None,
    ) -> ColumnSpec | None:
        if col_types := cls.get_column_types(native_type):
            column_type, generic_type = col_types
            is_dttm = generic_type == GenericDataType.TEMPORAL
            return ColumnSpec(
                sqla_type=column_type, generic_type=generic_type, is_dttm=is_dttm
            )
        return None

    @classmethod
    def get_sqla_column_type(
        cls,
        native_type: str | None,
        db_extra: dict[str, Any] | None = None,
        source: Any = None,
    ) -> TypeEngine | None:
        column_spec = cls.get_column_spec(
            native_type=native_type,
            db_extra=db_extra,
            source=source,
        )
        return column_spec.sqla_type if column_spec else None

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        return None

    @classmethod
    def extract_error_message(cls, ex: Exception) -> str:
        return f"{cls.engine} error: {cls._extract_error_message(ex)}"

    @classmethod
    def _extract_error_message(cls, ex: Exception) -> str:
        """Extract error message for queries.

        Sanitises two SQLAlchemy-2.0 + asyncpg artifacts that leak into
        the user-visible toast:

        * ``<class 'asyncpg.exceptions.XError'>: real-message`` — the
          DBAPIError repr prefix for wrapped asyncpg exceptions; strip.
        * ``\\n[SQL: ...]\\n[parameters: ...]`` — SA appends the offending
          query (including bound parameters!) to ``str(exc)``; trim
          everything from the first ``\\n[SQL:`` onward.
        """
        msg = ""
        if hasattr(ex, "message"):
            if isinstance(ex.message, dict):  # type: ignore[union-attr]
                msg = ex.message.get("message")  # type: ignore[union-attr]
            elif ex.message:  # type: ignore[union-attr]
                msg = ex.message  # type: ignore[union-attr]
        raw = str(msg) or str(ex)
        # Strip [SQL:] payload first (it may contain ``<class '…'>`` substrings
        # inside parameter values), then the leading exception-class repr.
        # Strip only newline artifacts, NOT spaces: several engine error regexes
        # (e.g. Athena/Presto ``Expecting: ``) match on a trailing space that
        # ``.strip()`` would remove, dropping the match.
        raw = raw.split("\n[SQL:")[0].strip("\n")
        import re as _re

        raw = _re.sub(r"<class '[^']+'>:\s*", "", raw)
        # SA 2.0 wraps empty DBAPI errors as ``(builtins.NoneType) None``;
        # collapse to just the message portion.
        raw = _re.sub(r"^\(builtins\.[A-Za-z_]+\)\s*", "", raw)
        if raw.lower() == "none":
            raw = ""
        return raw

    @classmethod
    def extract_errors(
        cls, ex: Exception, context: dict[str, Any] | None = None
    ) -> list[SupersetError]:
        raw_message = cls._extract_error_message(ex)

        context = context or {}
        for regex, (message, error_type, extra) in cls.custom_errors.items():
            if match := regex.search(raw_message):
                params = {**context, **match.groupdict()}
                extra["engine_name"] = cls.engine_name
                return [
                    SupersetError(
                        error_type=error_type,
                        message=message % params,
                        level=ErrorLevel.ERROR,
                        extra=extra,
                    )
                ]

        return [
            SupersetError(
                error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
                message=cls._extract_error_message(ex),
                level=ErrorLevel.ERROR,
                extra={"engine_name": cls.engine_name},
            )
        ]

    @classmethod
    def adjust_engine_params(
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        return uri, {
            **connect_args,
            **cls.enforce_uri_query_params.get(uri.get_driver_name(), {}),
        }

    @classmethod
    def get_prequeries(
        cls,
        database: Database,
        catalog: str | None = None,
        schema: str | None = None,
    ) -> list[str]:
        """Return SQL statements to run before a session
        (alternative to adjust_engine_params)."""
        return []

    @classmethod
    def get_catalog_names(
        cls,
        database: Database,
        inspector: Inspector,
    ) -> set[str]:
        return set()

    @classmethod
    def get_schema_names(cls, inspector: Inspector) -> set[str]:
        return set(inspector.get_schema_names())

    @classmethod
    def get_table_names(
        cls,
        database: Database,
        inspector: Inspector,
        schema: str | None,
    ) -> set[str]:
        try:
            tables = set(inspector.get_table_names(schema))
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex

        if schema and cls.try_remove_schema_from_table_name:
            tables = {re.sub(f"^{schema}\\.", "", table) for table in tables}
        return tables

    @classmethod
    def get_view_names(
        cls,
        database: Database,
        inspector: Inspector,
        schema: str | None,
    ) -> set[str]:
        try:
            views = set(inspector.get_view_names(schema))
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex

        if schema and cls.try_remove_schema_from_table_name:
            views = {re.sub(f"^{schema}\\.", "", view) for view in views}
        return views

    @classmethod
    def get_indexes(
        cls,
        database: Database,
        inspector: Inspector,
        table: Table,
    ) -> list[dict[str, Any]]:
        return inspector.get_indexes(table.table, table.schema)

    @classmethod
    def get_table_comment(
        cls,
        inspector: Inspector,
        table: Table,
    ) -> str | None:
        comment = None
        try:
            comment = inspector.get_table_comment(table.table, table.schema)
            comment = comment.get("text") if isinstance(comment, dict) else None
        except NotImplementedError:
            pass
        except Exception:
            logger.error("Unexpected error while fetching table comment", exc_info=True)
        return comment

    @classmethod
    def get_columns(
        cls,
        inspector: Inspector,
        table: Table,
        options: dict[str, Any] | None = None,
    ) -> list[ResultSetColumnType]:
        from typing import cast

        return convert_inspector_columns(
            cast(
                list[SQLAColumnType],
                inspector.get_columns(table.table, table.schema),
            )
        )

    @classmethod
    def get_metrics(
        cls,
        database: Database,
        inspector: Inspector,
        table: Table,
    ) -> list[MetricType]:
        return [
            {
                "metric_name": "count",
                "verbose_name": "COUNT(*)",
                "metric_type": "count",
                "expression": "COUNT(*)",
            }
        ]

    @classmethod
    def get_table_metadata(
        cls,
        database: Database,
        table: Table,
    ) -> dict[str, Any]:
        from superset.databases.utils import get_table_metadata

        return get_table_metadata(database, table)

    @classmethod
    def get_extra_table_metadata(
        cls,
        database: Database,
        table: Table,
    ) -> dict[str, Any]:
        """Return engine-specific table metadata.

        Falls back to the deprecated ``extra_table_metadata`` method if present.
        """
        # old method that doesn't work with catalogs
        if hasattr(cls, "extra_table_metadata"):
            warnings.warn(  # noqa: B028
                "The `extra_table_metadata` method is deprecated, please implement "
                "the `get_extra_table_metadata` method in the DB engine spec.",
                DeprecationWarning,
            )

            if table.catalog:
                return {}

            return cls.extra_table_metadata(database, table.table, table.schema)

        return {}

    @classmethod
    def get_limit_from_sql(cls, sql: str) -> int | None:
        script = SQLScript(sql, engine=cls.engine)
        return script.statements[-1].get_limit_value()

    @classmethod
    def get_cte_query(cls, sql: str) -> str | None:
        """Wrap a CTE query for virtual table conversion, or return None."""
        if not cls.allows_cte_in_subquery:
            statement = SQLStatement(sql, engine=cls.engine)
            if statement.has_cte():
                return statement.as_cte(cls.cte_alias).format()
        return None

    @classmethod
    def df_to_sql(
        cls,
        database: Any,
        table: Any,
        df: Any,
        to_sql_kwargs: dict[str, Any],
    ) -> None:
        """Upload a pandas DataFrame to a database table via ``to_sql``.

        Uses the database's SYNC engine (``get_sqla_engine`` — psycopg2 for
        the examples DB) because pandas ``to_sql`` is blocking sync IO; the
        caller (``UploadCommand.run``) MUST invoke this inside
        ``asyncio.to_thread`` so it doesn't block the event loop.

        Does NOT create SqlaTable metadata — that's the command's job.
        """
        to_sql_kwargs["name"] = table.table
        if table.schema:
            to_sql_kwargs["schema"] = table.schema

        with database.get_sqla_engine(
            catalog=getattr(table, "catalog", None),
            schema=table.schema,
        ) as engine:
            if (
                engine.dialect.supports_multivalues_insert
                or cls.supports_multivalues_insert
            ):
                to_sql_kwargs["method"] = "multi"
            df.to_sql(con=engine, **to_sql_kwargs)

    @classmethod
    def _get_fields(cls, cols: list[ResultSetColumnType]) -> list[Any]:
        return [
            (
                literal_column(query_as)
                if (query_as := c.get("query_as"))
                else column(c["column_name"])
            )
            for c in cols
        ]

    @classmethod
    def where_latest_partition(
        cls,
        database: Database,
        table: Table,
        query: Select,
        columns: list[ResultSetColumnType] | None = None,
    ) -> Select | None:
        """Add a where clause to reference only the most recent partition."""
        return None

    @classmethod
    def select_star(
        cls,
        database: Database,
        table: Table,
        engine: Engine,
        limit: int = 100,
        show_cols: bool = False,
        indent: bool = True,
        latest_partition: bool = True,
        cols: list[ResultSetColumnType] | None = None,
    ) -> str:
        """Generate a "SELECT * from [catalog.][schema.]table_name" query.

        WARNING: expects only unquoted table and schema names.
        """
        if not cls.supports_cross_catalog_queries:
            table = Table(table.table, table.schema, None)

        fields: str | list[Any] = "*"
        cols = cols or []
        if (show_cols or latest_partition) and not cols:
            cols = database.get_columns(table)

        if show_cols:
            fields = cls._get_fields(cols)

        full_table_name = cls.quote_table(table, engine.dialect)
        # SA 2.0 select() takes *args; ``"*"`` becomes a literal-column star.
        select_cols = [literal_column("*")] if isinstance(fields, str) else fields
        qry = select(*select_cols).select_from(text(full_table_name))

        qry = qry.limit(limit)
        if latest_partition:
            partition_query = cls.where_latest_partition(
                database,
                table,
                qry,
                columns=cols,
            )
            if partition_query is not None:
                qry = partition_query
        sql = database.compile_sqla_query(qry, table.catalog, table.schema)
        if indent:
            sql = SQLScript(sql, engine=cls.engine).format()
        return sql

    @classmethod
    def estimate_statement_cost(
        cls, database: Database, statement: str, cursor: Any
    ) -> dict[str, Any]:
        raise Exception("Database does not support cost estimation")  # noqa: TRY002

    @classmethod
    def query_cost_formatter(
        cls, raw_cost: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        raise Exception("Database does not support cost estimation")  # noqa: TRY002

    @classmethod
    def process_statement(
        cls,
        statement: Any,
        database: Database,
    ) -> str:
        return database.mutate_sql_based_on_config(str(statement), is_split=True)

    @classmethod
    def estimate_query_cost(
        cls,
        database: Database,
        catalog: str | None,
        schema: str,
        sql: str,
        source: Any = None,
    ) -> list[dict[str, Any]]:
        extra = database.get_extra(source) or {}
        if not cls.get_allow_cost_estimate(extra):
            raise Exception(  # noqa: TRY002
                "Database does not support cost estimation"
            )

        parsed_script = SQLScript(sql, engine=cls.engine)

        with database.get_raw_connection(
            catalog=catalog,
            schema=schema,
            source=source,
        ) as conn:
            cursor = conn.cursor()
            return [
                cls.estimate_statement_cost(
                    database,
                    cls.process_statement(statement, database),
                    cursor,
                )
                for statement in parsed_script.statements
            ]

    @staticmethod
    def _mutate_label(label: str) -> str:
        """No-op; engines override to enforce naming constraints (e.g. lowercase)."""
        return label

    @classmethod
    def _truncate_label(cls, label: str) -> str:
        label = md5_sha_from_str(label)
        if cls.max_column_name_length and len(label) > cls.max_column_name_length:
            label = label[: cls.max_column_name_length]
        return label

    @classmethod
    def make_label_compatible(cls, label: str) -> str | quoted_name:
        """Conditionally mutate and/or quote a sqlalchemy expression label.

        If ``force_column_alias_quotes`` is True, return the label as a
        ``quoted_name``. If the maximum supported column name length is
        exceeded, generate a truncated label.
        """
        label_mutated = cls._mutate_label(label)
        if (
            cls.max_column_name_length
            and len(label_mutated) > cls.max_column_name_length
        ):
            label_mutated = cls._truncate_label(label)
        if cls.force_column_alias_quotes:
            label_mutated = quoted_name(label_mutated, True)
        return label_mutated

    @classmethod
    def column_datatype_to_string(
        cls, sqla_column_type: TypeEngine, dialect: Dialect
    ) -> str:
        """Compile column type, stripping collation/charset to avoid verbose output."""
        sqla_column_type = sqla_column_type.copy()
        if hasattr(sqla_column_type, "collation"):
            sqla_column_type.collation = None
        if hasattr(sqla_column_type, "charset"):
            sqla_column_type.charset = None
        return sqla_column_type.compile(dialect=dialect).upper()

    @classmethod
    def prepare_cancel_query(cls, query: Query) -> None:
        return None

    @classmethod
    def has_implicit_cancel(cls) -> bool:
        return False

    @classmethod
    def get_cancel_query_id(
        cls,
        cursor: Any,
        query: Query,
    ) -> str | None:
        return None

    @classmethod
    def cancel_query(
        cls,
        cursor: Any,
        query: Query,
        cancel_query_id: str,
    ) -> bool:
        return False

    @classmethod
    def mask_encrypted_extra(cls, encrypted_extra: str | None) -> str | None:
        """Remove sensitive fields from ``encrypted_extra`` before
        presenting to user."""
        if encrypted_extra is None or not cls.encrypted_extra_sensitive_fields:
            return encrypted_extra

        try:
            config = _json.loads(encrypted_extra)
        except (TypeError, _json.JSONDecodeError):
            return encrypted_extra

        masked_encrypted_extra = redact_sensitive(
            config,
            cls.encrypted_extra_sensitive_fields,
        )

        return _json.dumps(masked_encrypted_extra)

    @classmethod
    def unmask_encrypted_extra(cls, old: str | None, new: str | None) -> str | None:
        """Restore masked values in *new* from *old*
        (allows password reuse on update)."""
        if old is None or new is None:
            return new

        try:
            old_config = _json.loads(old)
            new_config = _json.loads(new)
        except (TypeError, _json.JSONDecodeError):
            return new

        new_config = reveal_sensitive(
            old_config,
            new_config,
            cls.encrypted_extra_sensitive_fields,
        )

        return _json.dumps(new_config)

    @classmethod
    def get_public_information(cls) -> dict[str, Any]:
        return {
            "supports_file_upload": cls.supports_file_upload,
            "disable_ssh_tunneling": cls.disable_ssh_tunneling,
            "supports_dynamic_catalog": cls.supports_dynamic_catalog,
            "supports_oauth2": cls.supports_oauth2,
        }

    @classmethod
    def get_function_names(
        cls,
        database: Database,
    ) -> list[str]:
        """Return function names for SQL Lab autocomplete."""
        return []

    @staticmethod
    def mutate_db_for_connection_test(
        database: Database,
    ) -> None:
        """Hook to mutate the database object before a connection test."""
        return None

    @staticmethod
    def get_extra_params(database: Database, source: Any = None) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if database.extra:
            try:
                extra = _json.loads(database.extra)
            except _json.JSONDecodeError as ex:
                logger.error(ex, exc_info=True)
                raise
        return extra

    @staticmethod
    def update_params_from_encrypted_extra(
        database: Database, params: dict[str, Any]
    ) -> None:
        if not database.encrypted_extra:
            return
        try:
            encrypted_extra = _json.loads(database.encrypted_extra)
            params.update(encrypted_extra)
        except _json.JSONDecodeError as ex:
            logger.error(ex, exc_info=True)
            raise

    @classmethod
    def validate_database_uri(cls, sqlalchemy_uri: URL) -> None:
        """Validate URI via ``DB_SQLA_URI_VALIDATOR`` callback
        and disallowed-params check."""
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            db_engine_uri_validator = getattr(settings, "db_sqla_uri_validator", None)
        except Exception:  # noqa: BLE001
            db_engine_uri_validator = None

        if db_engine_uri_validator:
            db_engine_uri_validator(sqlalchemy_uri)

        if existing_disallowed := cls.disallow_uri_query_params.get(
            sqlalchemy_uri.get_driver_name(), set()
        ).intersection(sqlalchemy_uri.query):
            raise ValueError(f"Forbidden query parameter(s): {existing_disallowed}")

    @classmethod
    def denormalize_name(cls, dialect: Dialect, name: str) -> str:
        if (
            hasattr(dialect, "requires_name_normalize")
            and dialect.requires_name_normalize
        ):
            return dialect.denormalize_name(name)
        return name

    @classmethod
    def quote_table(cls, table: Table, dialect: Dialect) -> str:
        quoters = {
            "catalog": dialect.identifier_preparer.quote_schema,
            "schema": dialect.identifier_preparer.quote_schema,
            "table": dialect.identifier_preparer.quote,
        }

        return ".".join(
            function(getattr(table, key))
            for key, function in quoters.items()
            if getattr(table, key)
        )

    @classmethod
    def get_column_description_limit_size(cls) -> int:
        return 1

    @staticmethod
    def pyodbc_rows_to_tuples(data: list[Any]) -> list[tuple[Any, ...]]:
        if data and type(data[0]).__name__ == "Row":
            data = [tuple(row) for row in data]
        return data

    @classmethod
    def handle_boolean_filter(cls, sqla_col: Any, op: str, value: bool) -> Any:
        """Use IS or == for boolean filters per ``use_equality_for_boolean_filters``."""
        if cls.use_equality_for_boolean_filters:
            return sqla_col == value
        return sqla_col.is_(value)

    @classmethod
    def handle_null_filter(
        cls,
        sqla_col: Any,
        op: str,
    ) -> Any:
        op_upper = str(op).upper().replace("_", " ")
        if op_upper in ("IS NULL",):
            return sqla_col.is_(None)
        if op_upper in ("IS NOT NULL",):
            return sqla_col.isnot(None)
        raise ValueError(f"Invalid null filter operator: {op}")

    @classmethod
    def handle_comparison_filter(
        cls,
        sqla_col: Any,
        op: str,
        value: Any,
    ) -> Any:
        op_str = str(op)
        if op_str in ("==", "EQUALS"):
            return sqla_col == value
        if op_str in ("!=", "NOT_EQUALS"):
            return sqla_col != value
        if op_str in (">", "GREATER_THAN"):
            return sqla_col > value
        if op_str in ("<", "LESS_THAN"):
            return sqla_col < value
        if op_str in (">=", "GREATER_THAN_OR_EQUALS"):
            return sqla_col >= value
        if op_str in ("<=", "LESS_THAN_OR_EQUALS"):
            return sqla_col <= value
        raise ValueError(f"Invalid comparison filter operator: {op}")

    @classmethod
    def alter_new_orm_column(cls, orm_col: Any) -> None:
        """Hook to set default attributes on newly detected columns (e.g. is_dttm)."""


class BasicParametersType(TypedDict, total=False):
    username: str | None
    password: str | None
    host: str
    port: int
    database: str
    query: dict[str, Any]
    encryption: bool


class BasicPropertiesType(TypedDict):
    parameters: BasicParametersType


BASIC_PARAMETERS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": {
            "type": "string",
            "nullable": True,
            "description": "Username",
        },
        "password": {
            "type": "string",
            "nullable": True,
            "description": "Password",
        },
        "host": {
            "type": "string",
            "description": "Hostname or IP address",
        },
        "port": {
            "type": "integer",
            "format": "int32",
            "minimum": 0,
            "maximum": 65536,
            "exclusiveMaximum": True,
            "description": "Database port",
        },
        "database": {
            "type": "string",
            "description": "Database name",
        },
        "query": {
            "type": "object",
            "additionalProperties": {},
            "description": "Additional parameters",
        },
        "encryption": {
            "type": "boolean",
            "description": "Use an encrypted connection to the database",
        },
        "ssh": {
            "type": "boolean",
            "description": "Use an ssh tunnel connection to the database",
        },
    },
    "required": ["database", "host", "port", "username"],
}


class BasicParametersMixin:
    """Mixin for configuring engine specs via individual parameters
    instead of a raw URI.

    Handles the common ``engine+driver://user:password@host:port/dbname[?key=value...]``
    pattern.
    """

    parameters_schema: dict[str, Any] = BASIC_PARAMETERS_JSON_SCHEMA
    default_driver = ""
    # query parameter to enable encryption,
    # e.g. ``{"sslmode": "verify-ca"}`` for Postgres
    encryption_parameters: dict[str, str] = {}

    @classmethod
    def build_sqlalchemy_uri(  # pylint: disable=unused-argument
        cls,
        parameters: BasicParametersType,
        encrypted_extra: dict[str, str] | None = None,
    ) -> str:
        # TODO (betodealmeida): this method should also build `connect_args`
        query = parameters.get("query", {}).copy()
        if parameters.get("encryption"):
            if not cls.encryption_parameters:
                raise Exception(  # pylint: disable=broad-exception-raised  # noqa: TRY002
                    "Unable to build a URL with encryption enabled"
                )
            query.update(cls.encryption_parameters)

        # SA 2.x str(URL) masks the password; render_as_string(hide_password=False)
        # returns the full URI as SA 1.4 str(URL.create(...)) did.
        return URL.create(
            f"{cls.engine}+{cls.default_driver}".rstrip("+"),  # type: ignore[attr-defined]
            username=parameters.get("username"),
            password=parameters.get("password"),
            host=parameters["host"],
            port=parameters["port"],
            database=parameters["database"],
            query=query,
        ).render_as_string(hide_password=False)

    @classmethod
    def get_parameters_from_uri(  # pylint: disable=unused-argument
        cls, uri: str, encrypted_extra: dict[str, Any] | None = None
    ) -> BasicParametersType:
        url = make_url_safe(uri)
        query = {
            key: value
            for (key, value) in url.query.items()
            if (key, value) not in cls.encryption_parameters.items()
        }
        encryption = all(
            item in url.query.items() for item in cls.encryption_parameters.items()
        )
        return {
            "username": url.username,
            "password": url.password,
            "host": url.host,
            "port": url.port,
            "database": url.database,
            "query": query,
            "encryption": encryption,
        }

    @classmethod
    def validate_parameters(
        cls, properties: BasicPropertiesType
    ) -> list[SupersetError]:
        """Progressive validation: hostname-only → port → full params."""
        errors: list[SupersetError] = []

        required = {"host", "port", "username", "database"}
        parameters = properties.get("parameters", {})
        present = {key for key in parameters if parameters.get(key, ())}

        if missing := sorted(required - present):
            errors.append(
                SupersetError(
                    message=(
                        f"One or more parameters are missing: {', '.join(missing)}"
                    ),
                    error_type=SupersetErrorType.CONNECTION_MISSING_PARAMETERS_ERROR,
                    level=ErrorLevel.WARNING,
                    extra={"missing": missing},
                ),
            )

        host = parameters.get("host", None)
        if not host:
            return errors
        if not is_hostname_valid(host):
            errors.append(
                SupersetError(
                    message="The hostname provided can't be resolved.",
                    error_type=SupersetErrorType.CONNECTION_INVALID_HOSTNAME_ERROR,
                    level=ErrorLevel.ERROR,
                    extra={"invalid": ["host"]},
                ),
            )
            return errors

        port = parameters.get("port", None)
        if not port:
            return errors
        try:
            port = int(port)
        except (ValueError, TypeError):
            errors.append(
                SupersetError(
                    message="Port must be a valid integer.",
                    error_type=SupersetErrorType.CONNECTION_INVALID_PORT_ERROR,
                    level=ErrorLevel.ERROR,
                    extra={"invalid": ["port"]},
                ),
            )
        if not (isinstance(port, int) and 0 <= port < 2**16):
            errors.append(
                SupersetError(
                    message=(
                        "The port must be an integer between 0 and 65535 (inclusive)."
                    ),
                    error_type=SupersetErrorType.CONNECTION_INVALID_PORT_ERROR,
                    level=ErrorLevel.ERROR,
                    extra={"invalid": ["port"]},
                ),
            )
        elif not is_port_open(host, port):
            errors.append(
                SupersetError(
                    message="The port is closed.",
                    error_type=SupersetErrorType.CONNECTION_PORT_CLOSED_ERROR,
                    level=ErrorLevel.ERROR,
                    extra={"invalid": ["port"]},
                ),
            )

        return errors

    @classmethod
    def parameters_json_schema(cls) -> Any:
        return cls.parameters_schema or None


__all__ = [
    "BASIC_PARAMETERS_JSON_SCHEMA",
    "BaseEngineSpec",
    "BasicParametersMixin",
    "BasicParametersType",
    "BasicPropertiesType",
    "ColumnSpec",
    "ColumnTypeMapping",
    "GenericDBException",
    "GenericDataType",
    "MetricType",
    "ResultSetColumnType",
    "TimestampExpression",
    "convert_inspector_columns",
]
