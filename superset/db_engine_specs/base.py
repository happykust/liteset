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
"""BaseEngineSpec — synchronous engine spec base class.

Ported 1:1 from ``superset_old/db_engine_specs/base.py`` with the legacy
WSGI-stack imports removed.  Only the methods/attributes actually
referenced by the liteset codebase are included; OAuth2, file-upload,
impersonation, and other legacy-only helpers are intentionally omitted.
"""

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

# When connecting to a database it's hard to catch specific exceptions, since
# we support more than 50 different database drivers.  Usually the try/except
# block will catch the generic ``Exception`` class.  To make it clear that we
# know this is a necessary evil we create an alias and catch it instead.
GenericDBException = Exception


# ---------------------------------------------------------------------------
# Column-type mapping helpers
# ---------------------------------------------------------------------------

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
    """Type for metrics returned by ``get_metrics``."""

    metric_name: str
    expression: str
    verbose_name: str | None
    metric_type: str | None
    description: str | None
    d3format: str | None
    currency: str | None
    warning_text: str | None
    extra: str | None


# ---------------------------------------------------------------------------
# Time grains (ported 1:1 from superset_old/db_engine_specs/base.py:117-144)
# ---------------------------------------------------------------------------


class TimeGrain(NamedTuple):
    name: str  # TODO: redundant field, remove
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


# ---------------------------------------------------------------------------
# Timestamp expression
# ---------------------------------------------------------------------------


class TimestampExpression(ColumnClause):  # type: ignore[type-arg]
    def __init__(self, expr: str, col: ColumnClause, **kwargs: Any) -> None:
        """SQLAlchemy class that renders native column elements respecting
        engine-specific quoting rules as part of a string-based expression.

        :param expr: SQL expression with ``{col}`` denoting locations where
            the *col* object will be rendered.
        :param col: The target column.
        """
        super().__init__(expr, **kwargs)
        self.col = col

    @property
    def _constructor(self) -> type[ColumnClause]:
        # Needed to ensure that the column label is rendered correctly when
        # proxied to the outer query.
        # See https://github.com/sqlalchemy/sqlalchemy/issues/4730
        return ColumnClause


@compiles(TimestampExpression)
def compile_timegrain_expression(
    element: TimestampExpression, compiler: Compiled, **kwargs: Any
) -> str:
    return element.name.replace("{col}", compiler.process(element.col, **kwargs))


# ---------------------------------------------------------------------------
# ResultSetColumnType — dict shape returned by inspector/cursor
# ---------------------------------------------------------------------------


class ResultSetColumnType(TypedDict, total=False):
    column_name: str
    name: str
    type: Any
    # 1:1 with upstream superset_typing.py:73-82
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


# ---------------------------------------------------------------------------
# GenericDataType re-export (canonical location: superset.typing)
# ---------------------------------------------------------------------------

from superset.typing import GenericDataType  # noqa: E402

# ---------------------------------------------------------------------------
# BaseEngineSpec
# ---------------------------------------------------------------------------


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

    # Type-specific functions to mutate values received from the database.
    column_type_mutators: dict[TypeEngine, Callable[[Any], Any]] = {}

    # Does database support join-free timeslot grouping
    time_groupby_inline = False
    limit_method = LimitMethod.FORCE_LIMIT
    supports_multivalues_insert = False
    allows_joins = True
    allows_subqueries = True
    allows_alias_in_select = True
    allows_alias_in_orderby = True
    allows_sql_comments = True
    allows_escaped_colons = True

    # Whether ORDER BY clause can use aliases created in SELECT that are the
    # same as a source column.
    allows_alias_to_source_column = True

    # Whether ORDER BY clause must appear in SELECT
    # (if True, then it doesn't have to).
    allows_hidden_orderby_agg = True

    # Whether ORDER BY clause can use sql calculated expression
    allows_hidden_cc_in_orderby = False

    # Whether allow CTE as subquery or regular CTE
    allows_cte_in_subquery = True
    cte_alias = "__cte"

    # Disallowed connection query parameters by driver name
    disallow_uri_query_params: dict[str, set[str]] = {}

    # Whether to use equality operators (= true/false) instead of IS operators
    # for boolean filters.
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

    # Whether the engine supports file uploads.
    supports_file_upload = True

    # Is the DB engine spec able to change the default schema?
    supports_dynamic_schema = False

    # Does the DB support catalogs?
    supports_catalog = False

    # Can the catalog be changed on a per-query basis?
    supports_dynamic_catalog = False

    # Does the DB engine spec support cross-catalog queries?
    supports_cross_catalog_queries = False

    # Does the engine support OAuth 2.0?
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

    # Does the query id relate to the connection?
    has_query_id_before_execute = True

    # ------------------------------------------------------------------
    # OAuth2 (1:1 with superset_old/db_engine_specs/base.py)
    # ------------------------------------------------------------------

    @classmethod
    def is_oauth2_enabled(cls) -> bool:
        """Return True if the engine has a configured OAuth2 client."""
        from superset.utils.oauth2 import get_oauth2_clients

        return cls.supports_oauth2 and cls.engine_name in get_oauth2_clients()

    @classmethod
    def get_oauth2_config(cls) -> "Any | None":
        """Build the engine-spec-level OAuth2 client config.

        Returns ``None`` when no OAuth2 client is registered for this
        engine.  The config dict matches :class:`OAuth2ClientConfig` and
        is validated via :class:`OAuth2ClientConfigSchema` (1:1 with the
        marshmallow schema in the original).
        """
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
        """Build the URL the browser should open to start the OAuth2 dance."""
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
        """Exchange an authorization ``code`` for refresh/access tokens.

        Async port of the original — uses :class:`httpx.AsyncClient` instead
        of ``requests``.
        """
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
        """Refresh an expired access token."""
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
        """Refresh an expired access token (synchronous).

        Sync sibling of :meth:`get_oauth2_fresh_token` — 1:1 with upstream
        ``superset_old/db_engine_specs/base.py:get_oauth2_fresh_token`` (which
        is originally synchronous and uses ``requests``). Used by the sync
        OAuth2 refresh path (:func:`superset.utils.oauth2.sync_refresh_oauth2_token`)
        that runs from the (psycopg2) impersonation connection flow. Uses
        :class:`httpx.Client` rather than ``requests`` (not installed); mirrors
        the async one's body otherwise.
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
        """Return True if *ex* indicates OAuth2 authorization is required.

        Mirrors the original ``g and hasattr(g, "user") and isinstance(ex,
        cls.oauth2_exception)`` — the user-bound guard keeps Celery workers
        (no request user) from supplanting the original driver error with an
        OAuth2 dance no one can complete.  The async equivalent of the
        upstream ``g.user`` check is the request-scoped ContextVar.
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
        """Raise :class:`OAuth2RedirectError` to start the OAuth2 dance.

        1:1 with ``start_oauth2_dance`` in
        ``superset_old/db_engine_specs/base.py``
        — the original takes only ``database`` and reads ``user_id`` from
        the request-scoped ``g`` global plus the redirect URI from
        ``url_for("DatabaseRestApi.oauth2", _external=True)``.

        In liteset the user identity lives in a :class:`ContextVar` (set by
        :class:`AuthMiddleware`) and the absolute redirect URI is
        configurable via ``WEBDRIVER_BASEURL`` / ``DATABASE_OAUTH2_REDIRECT_URI``.
        Both can also be supplied explicitly by callers that have them in
        scope (e.g. :class:`DatabaseTestConnectionCommand`).

        The frontend catches the resulting :class:`OAuth2RedirectError`,
        opens a popup at the returned ``url``, and waits for the popup to
        ``postMessage`` back the auth code.  Once the user authorizes the
        access, the popup is redirected to ``/api/v1/database/oauth2/``
        (handled by :class:`DatabaseController.oauth2`), which exchanges
        the code for a token and stores it in
        ``database_user_oauth2_tokens``.
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

    # ------------------------------------------------------------------
    # RLS
    # ------------------------------------------------------------------

    @classmethod
    def get_rls_method(cls) -> RLSMethod:
        """Return the RLS method to be used for this engine.

        There are two ways to insert RLS: either replacing the table with a
        subquery that has the RLS, or appending the RLS to the ``WHERE``
        clause. The former is safer, but not supported in all databases.
        """
        return (
            RLSMethod.AS_SUBQUERY
            if cls.allows_subqueries and cls.allows_alias_in_select
            else RLSMethod.AS_PREDICATE
        )

    # ------------------------------------------------------------------
    # URL / backend matching
    # ------------------------------------------------------------------

    @classmethod
    def supports_url(cls, url: URL) -> bool:
        """Return True if the DB engine spec supports a given SQLAlchemy URL."""
        backend = url.get_backend_name()
        driver = url.get_driver_name()
        return cls.supports_backend(backend, driver)

    @classmethod
    def supports_backend(cls, backend: str, driver: str | None = None) -> bool:
        """Return True if the DB engine spec supports a given backend/driver."""
        if backend != cls.engine and backend not in cls.engine_aliases:
            return False
        if not cls.drivers or driver is None:
            return True
        return driver in cls.drivers

    @classmethod
    def get_impersonation_key(cls, user: Any | None) -> Any:
        """Construct an impersonation key — by default the given username.

        1:1 with ``superset_old/db_engine_specs/base.py:get_impersonation_key``.
        Used by the per-user query cache key (``CACHE_IMPERSONATION`` /
        ``CACHE_QUERY_BY_USER``) so cached results are not shared across users
        when impersonation is in effect.

        :param user: logged-in user
        :returns: username if a user is given, else ``None``
        """
        return user.username if user else None

    # ------------------------------------------------------------------
    # Catalog / schema helpers
    # ------------------------------------------------------------------

    @classmethod
    def get_default_catalog(
        cls,
        database: Database,
    ) -> str | None:
        """Return the default catalog for a given database."""
        return None

    @classmethod
    def get_default_schema(cls, database: Database, catalog: str | None) -> str | None:
        """Return the default schema for a catalog in a given database."""
        with database.get_inspector(catalog=catalog) as inspector:
            return inspector.default_schema_name

    @classmethod
    def get_schema_from_engine_params(
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> str | None:
        """Return the schema configured in a SQLAlchemy URI, if any."""
        return None

    @classmethod
    def get_default_schema_for_query(
        cls,
        database: Database,
        query: Any,
        template_params: dict[str, Any] | None = None,
    ) -> str | None:
        """Return the default schema for a given query.

        1:1 with ``get_default_schema_for_query`` in
        ``superset_old/db_engine_specs/base.py``
        (line 707). Used by access-control to determine the schema of
        unqualified table references inside SQL Lab queries:

        1. Dialects that allow per-query schema switching honour the
           query's own ``schema`` field;
        2. Dialects that hard-code the schema in the SQLAlchemy URI
           or ``connect_args`` use that;
        3. Otherwise, fall back to the database default.
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
        """Method for dynamic ``allows_alias_in_select``."""
        return cls.allows_alias_in_select

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    @classmethod
    def get_dbapi_exception_mapping(cls) -> dict[type[Exception], type[Exception]]:
        """Map driver-specific exceptions to Superset DBAPI exceptions."""
        return {}

    @classmethod
    def parse_error_exception(cls, exception: Exception) -> Exception:
        """Engine-specific parser method."""
        return exception

    @classmethod
    def get_dbapi_mapped_exception(cls, exception: Exception) -> Exception:
        """Get a superset custom DBAPI exception from the driver exception."""
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
        """Modify URL and/or engine kwargs to impersonate a different user.

        1:1 with ``superset_old/db_engine_specs/base.py`` (the ``@deprecated``
        markers are dropped — they only emit warnings).
        """
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
        """Return a modified URL with the username set (1:1 upstream)."""
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
        """Set engine-specific impersonation properties on ``connect_args``.

        1:1 upstream — base is a no-op; engines (Hive/Presto/…) override.
        """

    @classmethod
    def get_allow_cost_estimate(
        cls,
        extra: dict[str, Any],
    ) -> bool:
        return False

    # ------------------------------------------------------------------
    # Text clause helpers
    # ------------------------------------------------------------------

    @classmethod
    def get_text_clause(cls, clause: str) -> TextClause:
        """SQLAlchemy wrapper to ensure text clauses are escaped properly."""
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

    # ------------------------------------------------------------------
    # Time-grain expressions
    # ------------------------------------------------------------------

    @classmethod
    def get_timestamp_expr(
        cls,
        col: ColumnClause,
        pdf: str | None,
        time_grain: str | None,
    ) -> TimestampExpression:
        """Construct a TimestampExpression for use in a SQLAlchemy query.

        :param col: Target column for the TimestampExpression
        :param pdf: date format (seconds or milliseconds)
        :param time_grain: time grain, e.g. P1Y for 1 year
        :return: TimestampExpression object
        """
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

        # if epoch, translate to DATE using db specific conf
        if pdf == "epoch_s":
            time_expr = time_expr.replace("{col}", cls.epoch_to_dttm())
        elif pdf == "epoch_ms":
            time_expr = time_expr.replace("{col}", cls.epoch_ms_to_dttm())

        return TimestampExpression(time_expr, col, type_=col.type)

    @classmethod
    def _sort_time_grains(
        cls, val: tuple[str | None, str], index: int
    ) -> float | int | str:
        """Return an ordered time-based value of a portion of a time grain
        for sorting."""
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
        """Return a dict of all supported time grains including any potential
        added grains but excluding any potentially disabled grains.

        :return: All time grain expressions supported by the engine
        """
        # 1:1 with superset_old/db_engine_specs/base.py:get_time_grain_expressions
        # — merge any engine-specific addon expressions, then drop denylisted
        # grains (both config-driven; defaults are empty).
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
        """
        Generate a tuple of supported time grains.

        1:1 with ``superset_old/db_engine_specs/base.py:get_time_grains``.

        :return: All time grains supported by the engine
        """
        ret_list = []
        time_grains = builtin_time_grains.copy()
        _, _, time_grain_addons = _time_grain_config()
        time_grains.update(time_grain_addons)
        for duration, func in cls.get_time_grain_expressions().items():
            if duration in time_grains:
                name = time_grains[duration]
                ret_list.append(TimeGrain(name, _(name), func, duration))
        return tuple(ret_list)

    # ------------------------------------------------------------------
    # Epoch to datetime SQL expressions
    # ------------------------------------------------------------------

    @classmethod
    def epoch_to_dttm(cls) -> str:
        """SQL expression that converts epoch (seconds) to datetime.

        The reference column should be denoted as ``{col}`` in the return
        expression, e.g. ``FROM_UNIXTIME({col})``.
        """
        raise NotImplementedError()

    @classmethod
    def epoch_ms_to_dttm(cls) -> str:
        """SQL expression that converts epoch (milliseconds) to datetime."""
        return cls.epoch_to_dttm().replace("{col}", "({col}/1000)")

    # ------------------------------------------------------------------
    # Data fetch / execute
    # ------------------------------------------------------------------

    @classmethod
    def fetch_data(cls, cursor: Any, limit: int | None = None) -> list[tuple[Any, ...]]:
        """Fetch data from cursor.

        :param cursor: Cursor instance
        :param limit: Maximum number of rows to be returned by the cursor
        :return: Result of query
        """
        if cls.arraysize:
            cursor.arraysize = cls.arraysize
        try:
            if cls.limit_method == LimitMethod.FETCH_MANY and limit:
                return cursor.fetchmany(limit)
            data = cursor.fetchall()
            description = cursor.description or []
            # Create a mapping between column name and a mutator function to
            # normalize values with.  The first two items in the description
            # row are the column name and type.
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
        """Some engines support expanding nested fields.

        See implementation in Presto spec for details.
        """
        return columns, data, []

    @classmethod
    def execute(
        cls,
        cursor: Any,
        query: str,
        database: Database,
        **kwargs: Any,
    ) -> None:
        """Execute a SQL query.

        :param cursor: Cursor instance
        :param query: Query to execute
        :param database: Database instance
        """
        if cls.arraysize:
            cursor.arraysize = cls.arraysize
        try:
            cursor.execute(query)
        except Exception as ex:
            # 1:1 with ``superset_old/db_engine_specs/base.py::execute`` — on a
            # DB error, if OAuth2 is enabled for this database and the error
            # indicates authorization is required, start the OAuth2 dance (which
            # raises ``OAuth2RedirectError`` so the frontend re-authenticates)
            # before mapping the exception.
            #
            # Caveat: the port's :meth:`start_oauth2_dance` is declared ``async``
            # but performs no real ``await`` (it only assembles the authorization
            # URL and raises ``OAuth2RedirectError``). ``execute`` runs in a
            # synchronous worker thread, so we drive the coroutine to its single
            # step via ``.send(None)`` — the same technique used by the sync
            # SQL Lab path (``superset.tasks.sql_lab._check_for_oauth2``).
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
        """Handle a live cursor between the execute and fetchall calls.

        The flow works without this method doing anything, but it allows for
        handling the cursor and updating progress information in the query
        object.
        """

    @classmethod
    def execute_with_cursor(
        cls,
        cursor: Any,
        sql: str,
        query: Query,
    ) -> None:
        """Trigger execution of a query and handle the resulting cursor.

        For most implementations this just makes calls to ``execute`` and
        ``handle_cursor`` consecutively, but in some engines (e.g. Trino) we
        may need to handle client limitations such as lack of async support.
        """
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

    # ------------------------------------------------------------------
    # Data type introspection
    # ------------------------------------------------------------------

    @classmethod
    def get_datatype(cls, type_code: Any) -> str | None:
        """Change column type code from cursor description to string repr.

        :param type_code: Type code from cursor description
        :return: String representation of type code
        """
        if isinstance(type_code, str) and type_code != "":
            return type_code.upper()
        return None

    @classmethod
    def get_column_types(
        cls,
        column_type: str | None,
    ) -> tuple[TypeEngine, GenericDataType] | None:
        """Return a SQLAlchemy native column type and generic data type that
        corresponds to the column type defined in the data source.

        :param column_type: Column type returned by inspector
        :return: SQLAlchemy and generic Superset column types
        """
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
        """Get generic type related specs regarding a native column type.

        :param native_type: Native database type
        :param db_extra: The database extra object
        :param source: Type coming from the database table or cursor description
        :return: ColumnSpec object
        """
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
        """Convert native database type to SQLAlchemy column type.

        :param native_type: Native database type
        :param db_extra: The database extra object
        :param source: Type coming from the database table or cursor description
        :return: SQLAlchemy TypeEngine or None
        """
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
        """Convert a Python ``datetime`` object to a SQL expression.

        :param target_type: The target type of expression
        :param dttm: The datetime object
        :param db_extra: The database extra object
        :return: The SQL expression
        """
        return None

    # ------------------------------------------------------------------
    # Error message extraction
    # ------------------------------------------------------------------

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
        # Order matters: strip the [SQL:] payload first (it may contain
        # ``<class '…'>`` substrings inside parameter values), then the
        # leading exception-class repr.
        raw = raw.split("\n[SQL:")[0].strip()
        import re as _re

        raw = _re.sub(r"<class '[^']+'>:\s*", "", raw)
        # ``(builtins.NoneType) None`` / ``(builtins.ValueError) msg`` —
        # SA-2.0 wraps DBAPI errors with the builtin-class repr when the
        # underlying message is empty (e.g. an asyncpg ``Error`` with no
        # detail). The remaining ``(builtins.X) Y`` is noise; collapse
        # to just ``Y`` (or empty when Y is the literal ``None`` repr).
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

    # ------------------------------------------------------------------
    # Engine parameter adjustment
    # ------------------------------------------------------------------

    @classmethod
    def adjust_engine_params(
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        """Return a new URL and ``connect_args`` for a specific catalog/schema.

        This is used in SQL Lab, allowing users to select a schema from the
        list of schemas available in a given database, and have the query run
        with that schema as the default one.
        """
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
        """Return pre-session queries.

        These are currently used as an alternative to ``adjust_engine_params``
        for databases where the selected schema cannot be specified in the
        SQLAlchemy URI or connection arguments.
        """
        return []

    # ------------------------------------------------------------------
    # Inspector-based metadata
    # ------------------------------------------------------------------

    @classmethod
    def get_catalog_names(
        cls,
        database: Database,
        inspector: Inspector,
    ) -> set[str]:
        """Get all catalogs from database."""
        return set()

    @classmethod
    def get_schema_names(cls, inspector: Inspector) -> set[str]:
        """Get all schemas from database.

        :param inspector: SQLAlchemy inspector
        :return: All schemas in the database
        """
        return set(inspector.get_schema_names())

    @classmethod
    def get_table_names(
        cls,
        database: Database,
        inspector: Inspector,
        schema: str | None,
    ) -> set[str]:
        """Get all the real table names within the specified schema.

        :param database: The database to inspect
        :param inspector: The SQLAlchemy inspector
        :param schema: The schema to inspect
        :returns: The physical table names
        """
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
        """Get all the view names within the specified schema.

        :param database: The database to inspect
        :param inspector: The SQLAlchemy inspector
        :param schema: The schema to inspect
        :returns: The view names
        """
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
        """Get the indexes associated with the specified schema/table."""
        return inspector.get_indexes(table.table, table.schema)

    @classmethod
    def get_table_comment(
        cls,
        inspector: Inspector,
        table: Table,
    ) -> str | None:
        """Get comment of table from a given schema and table."""
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
        """Get all columns from a given schema and table.

        :param inspector: SQLAlchemy Inspector instance
        :param table: Table instance
        :param options: Extra options to customise the display of columns
        :return: All columns in table
        """
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
        """Get all metrics from a given schema and table."""
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
        """Returns basic table metadata.

        1:1 with ``superset_old/db_engine_specs/base.py:get_table_metadata``.
        Delegates to ``superset.databases.utils.get_table_metadata``.

        :param database: Database instance
        :param table: A Table instance
        :return: Basic table metadata
        """
        from superset.databases.utils import get_table_metadata

        return get_table_metadata(database, table)

    @classmethod
    def get_extra_table_metadata(
        cls,
        database: Database,
        table: Table,
    ) -> dict[str, Any]:
        """Returns engine-specific table metadata.

        1:1 with ``superset_old/db_engine_specs/base.py:get_extra_table_metadata``.
        Includes backwards-compat fallback for the deprecated
        ``extra_table_metadata`` method.

        :param database: Database instance
        :param table: A Table instance
        :return: Engine-specific table metadata
        """
        # old method that doesn't work with catalogs
        if hasattr(cls, "extra_table_metadata"):
            warnings.warn(  # noqa: B028
                "The `extra_table_metadata` method is deprecated, please implement "
                "the `get_extra_table_metadata` method in the DB engine spec.",
                DeprecationWarning,
            )

            # If a catalog is passed, return nothing, since we don't know the exact
            # table that is being requested.
            if table.catalog:
                return {}

            return cls.extra_table_metadata(database, table.table, table.schema)

        return {}

    # ------------------------------------------------------------------
    # SQL limit helpers
    # ------------------------------------------------------------------

    @classmethod
    def get_limit_from_sql(cls, sql: str) -> int | None:
        """Extract limit from SQL query.

        :param sql: SQL query
        :return: Value of limit clause in query
        """
        script = SQLScript(sql, engine=cls.engine)
        return script.statements[-1].get_limit_value()

    @classmethod
    def get_cte_query(cls, sql: str) -> str | None:
        """Convert the input CTE based SQL to the SQL for virtual table conversion.

        :param sql: SQL query
        :return: CTE with the main select query aliased as ``__cte``
        """
        if not cls.allows_cte_in_subquery:
            statement = SQLStatement(sql, engine=cls.engine)
            if statement.has_cte():
                return statement.as_cte(cls.cte_alias).format()
        return None

    # ------------------------------------------------------------------
    # SELECT * generation
    # ------------------------------------------------------------------

    @classmethod
    def df_to_sql(
        cls,
        database: Any,
        table: Any,
        df: Any,
        to_sql_kwargs: dict[str, Any],
    ) -> None:
        """Upload a pandas DataFrame to a database table via ``to_sql``.

        1:1 port of ``superset_old/db_engine_specs/base.py:1157``. Uses the
        database's SYNC engine (``get_sqla_engine`` — psycopg2 for the
        examples DB) because pandas ``to_sql`` is blocking sync IO; the
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
        qry = select(fields).select_from(text(full_table_name))

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

    # ------------------------------------------------------------------
    # Cost estimation
    # ------------------------------------------------------------------

    @classmethod
    def estimate_statement_cost(
        cls, database: Database, statement: str, cursor: Any
    ) -> dict[str, Any]:
        """Generate a SQL query that estimates the cost of a given statement.

        :param database: A Database object
        :param statement: A single SQL statement
        :param cursor: Cursor instance
        :return: Dictionary with different costs
        """
        raise Exception("Database does not support cost estimation")  # noqa: TRY002

    @classmethod
    def query_cost_formatter(
        cls, raw_cost: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Format cost estimate.

        :param raw_cost: Raw estimate from ``estimate_query_cost``
        :return: Human readable cost estimate
        """
        raise Exception("Database does not support cost estimation")  # noqa: TRY002

    @classmethod
    def process_statement(
        cls,
        statement: Any,
        database: Database,
    ) -> str:
        """Process a SQL statement by mutating it.

        :param statement: A single SQL statement
        :param database: Database instance
        :return: Processed SQL string
        """
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
        """Estimate the cost of a multiple statement SQL query.

        :param database: Database instance
        :param catalog: Database catalog
        :param schema: Database schema
        :param sql: SQL query with possibly multiple statements
        :param source: Source of the query (eg, "sql_lab")
        """
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

    # ------------------------------------------------------------------
    # Label / identifier handling
    # ------------------------------------------------------------------

    @staticmethod
    def _mutate_label(label: str) -> str:
        """Conditionally mutate a label.  Noop by default."""
        return label

    @classmethod
    def _truncate_label(cls, label: str) -> str:
        """Truncate a label that exceeds max length using md5 hash."""
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
        """Convert SQLAlchemy column type to string representation.

        Removes collation and character encoding info to avoid unnecessarily
        long datatypes.
        """
        sqla_column_type = sqla_column_type.copy()
        if hasattr(sqla_column_type, "collation"):
            sqla_column_type.collation = None
        if hasattr(sqla_column_type, "charset"):
            sqla_column_type.charset = None
        return sqla_column_type.compile(dialect=dialect).upper()

    # ------------------------------------------------------------------
    # Query cancellation
    # ------------------------------------------------------------------

    @classmethod
    def prepare_cancel_query(cls, query: Query) -> None:
        """Record cancelation intent so the query can be stopped."""
        return None

    @classmethod
    def has_implicit_cancel(cls) -> bool:
        """Return True if the live cursor handles implicit cancellation.

        :return: Whether the live cursor implicitly cancels the query
        """
        return False

    @classmethod
    def get_cancel_query_id(
        cls,
        cursor: Any,
        query: Query,
    ) -> str | None:
        """Select identifiers from the DB engine that uniquely identify the
        queries to cancel.

        :param cursor: Cursor instance
        :param query: Query instance
        :return: Query identifier
        """
        return None

    @classmethod
    def cancel_query(
        cls,
        cursor: Any,
        query: Query,
        cancel_query_id: str,
    ) -> bool:
        """Cancel query in the underlying database.

        :param cursor: New cursor instance to the db of the query
        :param query: Query instance
        :param cancel_query_id: Value returned by ``get_cancel_query_id``
        :return: True if query cancelled successfully, False otherwise
        """
        return False

    # ------------------------------------------------------------------
    # Encrypted extra masking
    # ------------------------------------------------------------------

    @classmethod
    def mask_encrypted_extra(cls, encrypted_extra: str | None) -> str | None:
        """Mask ``encrypted_extra``.

        This removes sensitive data in ``encrypted_extra`` when presenting it
        to the user when a database is edited.
        """
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
        """Remove masks from ``encrypted_extra``.

        This allows reusing existing values from the current encrypted extra on
        updates.  It's useful for reusing masked passwords, allowing keys to be
        updated without having to provide sensitive data to the client.
        """
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

    # ------------------------------------------------------------------
    # Public information
    # ------------------------------------------------------------------

    @classmethod
    def get_public_information(cls) -> dict[str, Any]:
        """Construct a dict with properties we want to expose."""
        return {
            "supports_file_upload": cls.supports_file_upload,
            "disable_ssh_tunneling": cls.disable_ssh_tunneling,
            "supports_dynamic_catalog": cls.supports_dynamic_catalog,
            "supports_oauth2": cls.supports_oauth2,
        }

    # ------------------------------------------------------------------
    # Function names (SQL Lab autocomplete)
    # ------------------------------------------------------------------

    @classmethod
    def get_function_names(
        cls,
        database: Database,
    ) -> list[str]:
        """Get a list of function names callable on the database.

        Used for SQL Lab autocomplete.
        """
        return []

    # ------------------------------------------------------------------
    # Connection test mutation
    # ------------------------------------------------------------------

    @staticmethod
    def mutate_db_for_connection_test(
        database: Database,
    ) -> None:
        """Mutate the database instance prior to testing the connection.

        Some databases require passing additional parameters for validating
        database connections.
        """
        return None

    # ------------------------------------------------------------------
    # Extra params
    # ------------------------------------------------------------------

    @staticmethod
    def get_extra_params(database: Database, source: Any = None) -> dict[str, Any]:
        """Extract extras from the database model.

        :param database: database instance from which to extract extras
        :param source: in which context is the connection needed
        """
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
        """Update params with sensitive information from encrypted_extra.

        :param database: database instance from which to extract extras
        :param params: params to be updated
        """
        if not database.encrypted_extra:
            return
        try:
            encrypted_extra = _json.loads(database.encrypted_extra)
            params.update(encrypted_extra)
        except _json.JSONDecodeError as ex:
            logger.error(ex, exc_info=True)
            raise

    # ------------------------------------------------------------------
    # URI validation
    # ------------------------------------------------------------------

    @classmethod
    def validate_database_uri(cls, sqlalchemy_uri: URL) -> None:
        """Validate a database SQLAlchemy URI per engine spec.

        1:1 with ``superset_old/db_engine_specs/base.py:validate_database_uri``.
        Invokes the user-configured ``DB_SQLA_URI_VALIDATOR`` callback (if set)
        before checking disallowed query params.
        """
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

    # ------------------------------------------------------------------
    # Dialect helpers
    # ------------------------------------------------------------------

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
        """Fully quote a table name, including the schema and catalog."""
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

    # ------------------------------------------------------------------
    # Column description limit size
    # ------------------------------------------------------------------

    @classmethod
    def get_column_description_limit_size(cls) -> int:
        """Get a minimum limit size for the sample SELECT column query
        to fetch the column metadata.
        """
        return 1

    @staticmethod
    def pyodbc_rows_to_tuples(data: list[Any]) -> list[tuple[Any, ...]]:
        """Convert pyodbc.Row objects from ``fetch_data`` to tuples."""
        if data and type(data[0]).__name__ == "Row":
            data = [tuple(row) for row in data]
        return data

    # ------------------------------------------------------------------
    # Boolean / null filter handling
    # ------------------------------------------------------------------

    @classmethod
    def handle_boolean_filter(cls, sqla_col: Any, op: str, value: bool) -> Any:
        """Handle boolean filter operations with engine-specific logic.

        By default uses SQLAlchemy's IS operator (column IS true/false).
        Engines that don't support IS for boolean values can override
        ``use_equality_for_boolean_filters``.
        """
        if cls.use_equality_for_boolean_filters:
            return sqla_col == value
        return sqla_col.is_(value)

    @classmethod
    def handle_null_filter(
        cls,
        sqla_col: Any,
        op: str,
    ) -> Any:
        """Handle null / not-null filter operations.

        :param sqla_col: SQLAlchemy column element
        :param op: Filter operator string (``"IS NULL"`` or ``"IS NOT NULL"``)
        :return: SQLAlchemy expression for the null filter
        """
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
        """Handle comparison filter operations (=, !=, >, <, >=, <=).

        :param sqla_col: SQLAlchemy column element
        :param op: Filter operator string
        :param value: Filter value
        :return: SQLAlchemy expression for the comparison filter
        """
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
        """Allow altering default column attributes when first detected.

        For instance special columns like ``__time`` for Druid can be set to
        ``is_dttm=True``.  Note that this only gets called when new columns
        are detected/created.
        """


# ---------------------------------------------------------------------------
# BasicParametersMixin — configures engine specs via a dict of parameters
# instead of a raw SQLAlchemy URI.  Ported 1:1 from
# ``superset_old/db_engine_specs/base.py`` (``class BasicParametersMixin``).
# ---------------------------------------------------------------------------


class BasicParametersType(TypedDict, total=False):
    """Typed dict describing the fields accepted by ``BasicParametersMixin``."""

    username: str | None
    password: str | None
    host: str
    port: int
    database: str
    query: dict[str, Any]
    encryption: bool


class BasicPropertiesType(TypedDict):
    """Top-level payload shape passed to ``validate_parameters``."""

    parameters: BasicParametersType


# The original code uses a Marshmallow ``Schema`` subclass here.  We don't
# ship Marshmallow in liteset, so ``parameters_schema`` becomes the JSON
# Schema dict directly: callers do ``hasattr(spec, "parameters_schema")``
# (still ``True``), check truthiness (a non-empty dict is truthy), and
# pass the value to ``parameters_json_schema()`` which returns it as-is.
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
    """
    Mixin for configuring DB engine specs via a dictionary.

    With this mixin the SQLAlchemy engine can be configured through
    individual parameters, instead of the full SQLAlchemy URI. This
    mixin is for the most common pattern of URI:

        engine+driver://user:password@host:port/dbname[?key=value&key=value...]

    """

    # JSON Schema describing the parameters used to configure the DB.  In the
    # original Apache Superset this was a Marshmallow ``Schema`` instance; in
    # liteset we attach the OpenAPI fragment directly so that
    # ``parameters_json_schema()`` is a no-op identity function.
    parameters_schema: dict[str, Any] = BASIC_PARAMETERS_JSON_SCHEMA

    # recommended driver name for the DB engine spec
    default_driver = ""

    # query parameter to enable encryption in the database connection
    # for Postgres this would be `{"sslmode": "verify-ca"}`, eg.
    encryption_parameters: dict[str, str] = {}

    @classmethod
    def build_sqlalchemy_uri(  # pylint: disable=unused-argument
        cls,
        parameters: BasicParametersType,
        encrypted_extra: dict[str, str] | None = None,
    ) -> str:
        # TODO (betodealmeida): this method should also build `connect_args`
        # make a copy so that we don't update the original
        query = parameters.get("query", {}).copy()
        if parameters.get("encryption"):
            if not cls.encryption_parameters:
                raise Exception(  # pylint: disable=broad-exception-raised  # noqa: TRY002
                    "Unable to build a URL with encryption enabled"
                )
            query.update(cls.encryption_parameters)

        # NOTE: In SQLAlchemy 2.x, ``str(URL)`` masks the password as
        # ``***``.  We must call ``render_as_string(hide_password=False)``
        # to get the full URI with the plain-text password, which is
        # what the original Apache Superset (SQLAlchemy 1.4) returned
        # via ``str(URL.create(...))``.
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
        """
        Validates any number of parameters, for progressive validation.

        If only the hostname is present it will check if the name is resolvable. As
        more parameters are present in the request, more validation is done.
        """
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
        """
        Return configuration parameters as OpenAPI.

        ``parameters_schema`` is itself a JSON Schema dict (see
        ``BASIC_PARAMETERS_JSON_SCHEMA`` above), so we return it as-is.
        """
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
