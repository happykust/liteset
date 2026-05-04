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
"""
superset.utils.core — public API surface for the Liteset port.

All symbols from superset_old/utils/core.py that are referenced by the
rest of the codebase are defined or re-exported here.  This module has
NO Flask imports and NO synchronous DB calls in the request path.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import tempfile
import threading
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum, StrEnum
from types import TracebackType
from typing import Any, cast, NamedTuple, Optional, TypedDict, TypeVar

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.sql.type_api import Variant

from superset.constants import (
    EXTRA_FORM_DATA_APPEND_KEYS,
    EXTRA_FORM_DATA_OVERRIDE_EXTRA_KEYS,
    EXTRA_FORM_DATA_OVERRIDE_REGULAR_MAPPINGS,
    NO_TIME_RANGE,
)

# ``GenericDataType`` originally lived in ``superset.utils.core``; the
# Liteset port hoisted it to ``superset.typing`` to keep core lean.  We
# re-export it here so legacy import paths keep working — multiple
# subsystems (csv, excel, viz) reference ``superset.utils.core.GenericDataType``.
from superset.typing import GenericDataType  # noqa: F401
from superset.utils.hashing import md5_sha_from_dict, md5_sha_from_str

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JS_MAX_INTEGER — largest integer JavaScript can handle (2^53-1)
# Ported 1:1 from superset_old/utils/core.py:126
# ---------------------------------------------------------------------------
JS_MAX_INTEGER = 9007199254740991  # Largest int JavaScript can handle 2^53-1

# ---------------------------------------------------------------------------
# Type aliases (originally from superset.superset_typing / superset.utils.core)
# ---------------------------------------------------------------------------
FormData = dict[str, Any]

T = TypeVar("T")


class AdhocFilterClause(TypedDict, total=False):
    """TypedDict for adhoc filter clauses — ported 1:1 from superset_old/utils/core.py:216."""

    clause: str
    expressionType: str
    filterOptionName: Optional[str]
    comparator: Any
    operator: str
    subject: str
    isExtra: Optional[bool]
    sqlExpression: Optional[str]


class QueryObjectFilterClause(TypedDict, total=False):
    """TypedDict for query object filter clauses — ported 1:1 from superset_old/utils/core.py:227."""

    col: Any
    op: str
    val: Any
    grain: Optional[str]
    isExtra: Optional[bool]


# ---------------------------------------------------------------------------
# generic_find_constraint_name  (uses Flask-SQLAlchemy db object)
# ---------------------------------------------------------------------------
def generic_find_constraint_name(
    table: str, columns: set[str], referenced: str, database: Any
) -> str | None:
    """Utility to find a constraint name in alembic migrations."""
    tbl = sa.Table(
        table, database.metadata, autoload=True, autoload_with=database.engine
    )

    for fk in tbl.foreign_key_constraints:
        if fk.referred_table.name == referenced and set(fk.column_keys) == columns:
            return fk.name  # type: ignore[return-value]

    return None


# ---------------------------------------------------------------------------
# generic_find_fk_constraint_name
# ---------------------------------------------------------------------------
def generic_find_fk_constraint_name(
    table: str, columns: set[str], referenced: str, insp: Inspector
) -> str | None:
    """Utility to find a foreign-key constraint name in alembic migrations."""
    for fk in insp.get_foreign_keys(table):
        if (
            fk["referred_table"] == referenced
            and set(fk["referred_columns"]) == columns
        ):
            return fk["name"]

    return None


# ---------------------------------------------------------------------------
# generic_find_fk_constraint_names
# ---------------------------------------------------------------------------
def generic_find_fk_constraint_names(
    table: str, columns: set[str], referenced: str, insp: Inspector
) -> set[str]:
    """Utility to find foreign-key constraint names in alembic migrations."""
    names: set[str] = set()

    for fk in insp.get_foreign_keys(table):
        if (
            fk["referred_table"] == referenced
            and set(fk["referred_columns"]) == columns
        ):
            if fk["name"] is not None:
                names.add(fk["name"])

    return names


# ---------------------------------------------------------------------------
# generic_find_uq_constraint_name
# ---------------------------------------------------------------------------
def generic_find_uq_constraint_name(
    table: str, columns: set[str], insp: Inspector
) -> str | None:
    """Utility to find a unique constraint name in alembic migrations."""
    for uq in insp.get_unique_constraints(table):
        if columns == set(uq["column_names"]):
            return uq["name"]

    return None


# ---------------------------------------------------------------------------
# Current-user / logs-context ContextVars
# ---------------------------------------------------------------------------
# Declared early so :func:`get_user_id` (used by event-logger code paths
# that import this module standalone) can resolve the bound user.
_current_user_ctx: ContextVar[Any] = ContextVar("_current_user_ctx", default=None)

# Form-data ContextVar — direct port of the original ``g.form_data`` slot
# that ``superset_old/tasks/async_queries.py::set_form_data`` populated
# and ``superset_old/jinja_context.py::get_dataset_id_from_context``
# read as a fallback.  Lives in ``utils.core`` (not ``jinja_context``)
# because non-template code paths (Celery tasks, warm-up cache command)
# must be able to set it without pulling in the Jinja module.
_current_form_data_ctx: ContextVar[Any] = ContextVar(
    "_current_form_data_ctx", default=None
)


# ---------------------------------------------------------------------------
# get_user_id  (port of superset_old/utils/core.py:get_user_id)
# ---------------------------------------------------------------------------
def get_user_id() -> int | None:
    """Return the ID of the current user, or ``None`` if unset.

    Port of ``superset_old.utils.core.get_user_id`` which read
    ``g.user.id``.  In Liteset the user is held on a :class:`ContextVar`
    populated by the auth middleware; this helper digs the ``id`` out of
    whichever object the middleware put there.

    Returns ``None`` when no user has been bound to the current async
    task — the canonical case during alembic migrations, Celery tasks
    that run outside of an authenticated request, or unit tests that
    never call :func:`set_current_user`.
    """
    try:
        user = _current_user_ctx.get(None)
    except LookupError:
        return None
    if user is None:
        return None
    user_id = getattr(user, "id", None)
    if user_id is None:
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# MediumText / LongText
# ---------------------------------------------------------------------------
def MediumText() -> Variant[Any]:  # noqa: N802
    return Text().with_variant(MEDIUMTEXT(), "mysql")  # type: ignore[return-value]


def LongText() -> Variant[Any]:  # noqa: N802
    return Text().with_variant(LONGTEXT(), "mysql")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# shortid
# ---------------------------------------------------------------------------
def shortid() -> str:
    return f"{uuid.uuid4()}"[-12:]


# ---------------------------------------------------------------------------
# as_list
# ---------------------------------------------------------------------------
def as_list(x: T | list[T]) -> list[T]:
    """
    Wrap an object in a list if it's not a list.

    :param x: The object
    :returns: A list wrapping the object if it's not already a list
    """
    return x if isinstance(x, list) else [x]


# ---------------------------------------------------------------------------
# Filter conversion helpers (used by adhoc_filters migration)
# ---------------------------------------------------------------------------
def simple_filter_to_adhoc(
    filter_clause: dict[str, Any],
    clause: str = "where",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "clause": clause.upper(),
        "expressionType": "SIMPLE",
        "comparator": filter_clause.get("val"),
        "operator": filter_clause["op"],
        "subject": cast(str, filter_clause["col"]),
    }
    if filter_clause.get("isExtra"):
        result["isExtra"] = True
    result["filterOptionName"] = md5_sha_from_dict(result)
    return result


def form_data_to_adhoc(form_data: dict[str, Any], clause: str) -> dict[str, Any]:
    if clause not in ("where", "having"):
        raise ValueError(f"Unsupported clause type: {clause}")
    result: dict[str, Any] = {
        "clause": clause.upper(),
        "expressionType": "SQL",
        "sqlExpression": form_data.get(clause),
    }
    result["filterOptionName"] = md5_sha_from_dict(result)
    return result


def convert_legacy_filters_into_adhoc(
    form_data: FormData,
) -> None:
    if not form_data.get("adhoc_filters"):
        adhoc_filters: list[dict[str, Any]] = []
        form_data["adhoc_filters"] = adhoc_filters

        for clause in ("having", "where"):
            if clause in form_data and form_data[clause] != "":
                adhoc_filters.append(form_data_to_adhoc(form_data, clause))

        if "filters" in form_data:
            adhoc_filters.extend(
                simple_filter_to_adhoc(fltr, "where")
                for fltr in form_data["filters"]
                if fltr is not None
            )

    for key in ("filters", "having", "where"):
        if key in form_data:
            del form_data[key]


def split_adhoc_filters_into_base_filters(
    form_data: FormData,
    engine: str,
) -> None:
    """
    Mutates form data to restructure the adhoc filters in the form of the three
    base filters, ``where``, ``having``, and ``filters`` which represent free
    form where sql, free form having sql, and structured where clauses.
    """
    adhoc_filters = form_data.get("adhoc_filters")
    if isinstance(adhoc_filters, list):
        simple_where_filters = []
        sql_where_filters = []
        sql_having_filters = []
        for adhoc_filter in adhoc_filters:
            expression_type = adhoc_filter.get("expressionType") or adhoc_filter.get(
                "expression_type"
            )
            clause = adhoc_filter.get("clause")
            if expression_type == "SIMPLE":
                if clause == "WHERE":
                    simple_where_filters.append(
                        {
                            "col": adhoc_filter.get("subject"),
                            "op": adhoc_filter.get("operator"),
                            "val": adhoc_filter.get("comparator"),
                        }
                    )
            elif expression_type == "SQL":
                sql_expression = adhoc_filter.get("sqlExpression") or adhoc_filter.get(
                    "sql_expression"
                )
                # sanitize_clause is not available in the migration shim;
                # migrations that call this function operate on already-stored
                # data, so we pass the expression through unchanged.
                if clause == "WHERE":
                    sql_where_filters.append(sql_expression)
                elif clause == "HAVING":
                    sql_having_filters.append(sql_expression)
        form_data["where"] = " AND ".join([f"({sql})" for sql in sql_where_filters])
        form_data["having"] = " AND ".join([f"({sql})" for sql in sql_having_filters])
        form_data["filters"] = simple_where_filters


# ---------------------------------------------------------------------------
# FilterOperator enum (ported from superset_old/utils/core.py)
# ---------------------------------------------------------------------------
class FilterOperator(StrEnum):
    """Operators used filter controls"""

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


class RowLevelSecurityFilterType(StrEnum):
    """Type of an RLS filter — ported 1:1 from the original Superset.

    See ``superset_old/utils/core.py``.
    - ``REGULAR``: filter applies when the user holds one of the listed roles.
    - ``BASE``: filter applies to everyone *except* users holding the listed
      roles (typically used to exempt Admin from a global filter).
    """

    REGULAR = "Regular"
    BASE = "Base"


class FilterStringOperators(StrEnum):
    EQUALS = "EQUALS"
    NOT_EQUALS = "NOT_EQUALS"
    LESS_THAN = "LESS_THAN"
    GREATER_THAN = "GREATER_THAN"
    LESS_THAN_OR_EQUAL = "LESS_THAN_OR_EQUAL"
    GREATER_THAN_OR_EQUAL = "GREATER_THAN_OR_EQUAL"
    IN = "IN"
    NOT_IN = "NOT_IN"
    ILIKE = "ILIKE"
    LIKE = "LIKE"
    IS_NOT_NULL = "IS_NOT_NULL"
    IS_NULL = "IS_NULL"
    LATEST_PARTITION = "LATEST_PARTITION"
    IS_TRUE = "IS_TRUE"
    IS_FALSE = "IS_FALSE"


# ---------------------------------------------------------------------------
# LoggerLevel  (ported 1:1 from superset_old/utils/core.py:194)
# ---------------------------------------------------------------------------
class LoggerLevel(StrEnum):
    """Logger method names — used by ``utils.log.get_logger_from_status``."""

    INFO = "info"
    WARNING = "warning"
    EXCEPTION = "exception"


# ---------------------------------------------------------------------------
# DatasourceType  (ported 1:1 from superset_old/utils/core.py)
# ---------------------------------------------------------------------------
class DatasourceType(StrEnum):
    """Type of a Superset data source — used by cache_manager,
    explore-form-data cache, and a number of legacy controllers.
    """

    TABLE = "table"
    DATASET = "dataset"
    QUERY = "query"
    SAVEDQUERY = "saved_query"
    VIEW = "view"


# ---------------------------------------------------------------------------
# to_int  (ported 1:1 from superset_old/utils/core.py:1931)
# ---------------------------------------------------------------------------
def to_int(v: Any, value_if_invalid: int = 0) -> int:
    """Coerce ``v`` to ``int`` returning a fallback on failure."""
    try:
        return int(v)
    except (ValueError, TypeError):
        return value_if_invalid


# ---------------------------------------------------------------------------
# error_msg_from_exception  (ported 1:1 from superset_old/utils/core.py:455)
# ---------------------------------------------------------------------------
def error_msg_from_exception(ex: Exception) -> str:
    """Translate an exception into a human-readable error message.

    Database drivers expose error info in different ways; this function
    inspects ``ex.message`` (which may be a dict) and falls back to
    ``str(ex)`` when nothing more specific is available.
    """
    msg: Any = ""
    if hasattr(ex, "message"):
        if isinstance(ex.message, dict):
            msg = ex.message.get("message")
        elif ex.message:
            msg = ex.message
    return str(msg) or str(ex)


# ---------------------------------------------------------------------------
# logs_context  --  ContextVar replacing legacy ``flask.g.logs_context``
# (used by superset.utils.decorators:logs_context)
# ---------------------------------------------------------------------------
_logs_context_ctx: ContextVar[dict[str, Any] | None] = ContextVar(
    "_logs_context_ctx", default=None
)


def get_logs_context() -> dict[str, Any]:
    """Return the per-task logs-context dict, initialising it on first read.

    Mirrors the original ``flask.g.logs_context`` behaviour: callers expect
    a *mutable* dict that survives the duration of the running request /
    Celery task.
    """
    ctx = _logs_context_ctx.get()
    if ctx is None:
        ctx = {}
        _logs_context_ctx.set(ctx)
    return ctx


def reset_logs_context() -> None:
    """Reset the logs-context for the current async task."""
    _logs_context_ctx.set(None)


# ---------------------------------------------------------------------------
# QuerySource / get_user_agent (ported from superset_old/utils/core.py)
# Used by DB engine specs (e.g. Databricks) to stamp connections with
# identifying user-agent strings.
# ---------------------------------------------------------------------------
class QuerySource(Enum):
    """
    The source of a SQL query.
    """

    CHART = 0
    DASHBOARD = 1
    SQL_LAB = 2


def get_user_agent(database: Any, source: QuerySource | None) -> str:
    """
    Return the user-agent to advertise when connecting to ``database``.

    Ported 1:1 from ``superset_old/utils/core.py``.  The original reads
    ``USER_AGENT_FUNC`` from Flask's ``current_app.config``; in liteset we
    resolve it via the pydantic settings module instead, but the behaviour
    is byte-for-byte equivalent.
    """
    # pylint: disable=import-outside-toplevel
    from superset.constants import DEFAULT_USER_AGENT

    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        user_agent_func = getattr(settings, "user_agent_func", None)
    except Exception:  # noqa: BLE001
        user_agent_func = None

    if user_agent_func is not None:
        return user_agent_func(database, source)

    return DEFAULT_USER_AGENT


# ---------------------------------------------------------------------------
# User context helpers (request-free versions for use in jinja templates)
#
# In the original Superset these read from Flask's ``g`` object. In Liteset
# the request context is threaded via Litestar's dependency injection.  For
# code paths that do *not* have access to a ``Request`` (e.g. Celery tasks,
# Jinja template rendering triggered outside a controller) we use a
# context-var based approach (the underlying ``_current_user_ctx`` is
# declared near the top of this module so :func:`get_user_id` can use it).
# ---------------------------------------------------------------------------


def set_current_user(user: Any) -> None:
    """Set the current user for the running async context."""
    _current_user_ctx.set(user)


def get_current_user() -> Any:
    """Return the current user or ``None``."""
    return _current_user_ctx.get(None)


def set_form_data(form_data: dict[str, Any]) -> Any:
    """Bind ``form_data`` to the current async task; return a reset token.

    Direct port of ``superset_old/tasks/async_queries.py::set_form_data``
    (and the equivalent line in ``commands/chart/warm_up_cache.py``)
    which assigned ``g.form_data = form_data`` so that
    ``jinja_context.get_dataset_id_from_context`` could later resolve
    the dataset id from the running task's form payload.

    Returns the :class:`ContextVar` token so callers can reset the
    binding in a ``finally`` clause and avoid leaking form data
    across Celery tasks that share an event loop.
    """
    return _current_form_data_ctx.set(form_data)


def get_form_data() -> dict[str, Any]:
    """Return the form_data bound to the current async task, or ``{}``.

    Mirrors the original ``getattr(g, "form_data", {})`` fallback used
    by ``jinja_context.get_dataset_id_from_context``.  Always returns
    a dict so callers can use ``.get(...)`` without a None-check.
    """
    value = _current_form_data_ctx.get(None)
    return value if isinstance(value, dict) else {}


def reset_form_data(token: Any) -> None:
    """Reset the form_data binding using the token returned by
    :func:`set_form_data`.

    Falls back to a hard ``set(None)`` if the token has gone stale
    (e.g. it was produced in a different task than the cleanup hook);
    we never want a stale form_data to leak across tasks.
    """
    try:
        _current_form_data_ctx.reset(token)
    except (LookupError, ValueError):
        _current_form_data_ctx.set(None)


# ---------------------------------------------------------------------------
# current_request  --  ContextVar that holds the in-flight Litestar Request.
#
# The original Apache Superset relied on Flask's thread-local ``request``
# proxy.  In our async port we instead bind the active request to a
# :class:`ContextVar` from a tiny ASGI middleware
# (``superset.middleware.request_context``) so that code paths which lack
# direct access to the request (deep inside Commands, audit-logging
# decorators, etc.) can still observe per-request fields like the
# ``Referer`` header or query string without having to thread the request
# through every signature.
#
# ``ContextVar`` semantics give us automatic per-task isolation: concurrent
# requests served by the same event loop never see each other's bindings,
# and Celery / CLI call sites that have no inbound request simply observe
# ``None`` (which matches the original ``has_request_context() is False``
# branch in ``superset_old/utils/log.py``).
# ---------------------------------------------------------------------------
_current_request_ctx: ContextVar[Any] = ContextVar("_current_request_ctx", default=None)


def set_current_request(request: Any) -> Any:
    """Bind ``request`` to the current async task; return a reset token.

    Called by :class:`superset.middleware.request_context.RequestContextMiddleware`
    on every inbound HTTP request.  Returns the token produced by
    ``ContextVar.set`` so the middleware can pass it to
    :func:`reset_current_request` once the response has been sent.
    """
    return _current_request_ctx.set(request)


def get_current_request() -> Any:
    """Return the request bound to the current async task, or ``None``."""
    return _current_request_ctx.get(None)


def reset_current_request(token: Any) -> None:
    """Reset the request binding using the token returned by
    :func:`set_current_request`.

    Falls back to a hard ``set(None)`` if the token has gone stale (e.g.
    the middleware ran in a different task than the cleanup hook); we
    never want a stale request to leak across requests.
    """
    try:
        _current_request_ctx.reset(token)
    except (LookupError, ValueError):
        _current_request_ctx.set(None)


def get_username() -> str | None:
    try:
        user = get_current_user()
        return user.username if user else None
    except Exception:
        return None


def get_user_email() -> str | None:
    try:
        user = get_current_user()
        return user.email if user else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# merge_extra_form_data / merge_extra_filters
# Ported 1:1 from superset_old/utils/core.py
# ---------------------------------------------------------------------------


def merge_extra_form_data(form_data: dict[str, Any]) -> None:  # noqa: C901
    """
    Merge extra form data (appends and overrides) into the main payload
    and add applied time extras to the payload.
    """
    filter_keys = ["filters", "adhoc_filters"]
    extra_form_data = form_data.pop("extra_form_data", {})
    append_filters: list[QueryObjectFilterClause] = extra_form_data.get("filters", None)

    # merge append extras
    for key in [key for key in EXTRA_FORM_DATA_APPEND_KEYS if key not in filter_keys]:
        extra_value = getattr(extra_form_data, key, {})
        form_value = getattr(form_data, key, {})
        form_value.update(extra_value)
        if form_value:
            form_data["key"] = extra_value

    # map regular extras that apply to form data properties
    for src_key, target_key in EXTRA_FORM_DATA_OVERRIDE_REGULAR_MAPPINGS.items():
        value = extra_form_data.get(src_key)
        if value is not None:
            form_data[target_key] = value

    # map extras that apply to form data extra properties
    extras = form_data.get("extras", {})
    for key in EXTRA_FORM_DATA_OVERRIDE_EXTRA_KEYS:
        value = extra_form_data.get(key)
        if value is not None:
            extras[key] = value
    if extras:
        form_data["extras"] = extras

    adhoc_filters: list[AdhocFilterClause] = form_data.get("adhoc_filters", [])
    form_data["adhoc_filters"] = adhoc_filters
    append_adhoc_filters: list[AdhocFilterClause] = extra_form_data.get(
        "adhoc_filters", []
    )
    adhoc_filters.extend(
        cast("AdhocFilterClause", {"isExtra": True, **adhoc_filter})
        for adhoc_filter in append_adhoc_filters
    )
    if append_filters:
        for key, value in form_data.items():
            if re.match("adhoc_filter.*", key):
                value.extend(
                    simple_filter_to_adhoc({"isExtra": True, **fltr})
                    for fltr in append_filters
                    if fltr
                )
    if form_data.get("time_range") and not form_data.get("granularity_sqla"):
        for adhoc_filter in form_data.get("adhoc_filters", []):
            if adhoc_filter.get("operator") == "TEMPORAL_RANGE":
                adhoc_filter["comparator"] = form_data["time_range"]


def merge_extra_filters(form_data: dict[str, Any]) -> None:  # noqa: C901
    """
    Merge extra_filters (temporary/contextual filters using legacy constructs)
    into the main payload.
    """
    form_data.setdefault("applied_time_extras", {})
    adhoc_filters = form_data.get("adhoc_filters", [])
    form_data["adhoc_filters"] = adhoc_filters
    merge_extra_form_data(form_data)
    if "extra_filters" in form_data:
        date_options = {
            "__time_range": "time_range",
            "__time_col": "granularity_sqla",
            "__time_grain": "time_grain_sqla",
        }

        def get_filter_key(f: dict[str, Any]) -> str:
            if "expressionType" in f:
                return f"{f['subject']}__{f['operator']}"
            return f"{f['col']}__{f['op']}"

        existing_filters = {}
        for existing in adhoc_filters:
            if (
                existing["expressionType"] == "SIMPLE"
                and existing.get("comparator") is not None
                and existing.get("subject") is not None
            ):
                existing_filters[get_filter_key(existing)] = existing["comparator"]

        for filtr in form_data["extra_filters"]:
            filtr["isExtra"] = True
            filter_column = filtr["col"]
            if time_extra := date_options.get(filter_column):
                time_extra_value = filtr.get("val")
                if time_extra_value and time_extra_value != NO_TIME_RANGE:
                    form_data[time_extra] = time_extra_value
                    form_data["applied_time_extras"][filter_column] = time_extra_value
            elif filtr["val"]:
                if (filter_key := get_filter_key(filtr)) in existing_filters:
                    if isinstance(filtr["val"], list):
                        if isinstance(existing_filters[filter_key], list):
                            if set(existing_filters[filter_key]) != set(filtr["val"]):
                                adhoc_filters.append(simple_filter_to_adhoc(filtr))
                        else:
                            adhoc_filters.append(simple_filter_to_adhoc(filtr))
                    else:
                        if filtr["val"] != existing_filters[filter_key]:
                            adhoc_filters.append(simple_filter_to_adhoc(filtr))
                else:
                    adhoc_filters.append(simple_filter_to_adhoc(filtr))
        del form_data["extra_filters"]


def merge_request_params(form_data: dict[str, Any], params: dict[str, Any]) -> None:
    """Merge request parameters to ``url_params`` in form_data.

    Only updates or appends parameters to ``form_data`` that are defined
    in ``params``; pre-existing parameters not in ``params`` are left
    unchanged.

    1:1 port of ``superset_old/utils/core.py:merge_request_params``.
    """
    url_params = form_data.get("url_params", {})
    for key, value in params.items():
        if key in ("form_data", "r"):
            continue
        url_params[key] = value
    form_data["url_params"] = url_params


class DatasourceName(NamedTuple):
    """Tuple shape used by ``Database.get_all_table_names_in_schema``.

    1:1 with ``superset_old.utils.core.DatasourceName`` — the
    ``TablesDatabaseCommand`` wraps each ``(table, schema, catalog)``
    triple from the (cached) inspector call so downstream code can
    address ``.table`` / ``.schema`` / ``.catalog`` by name.
    """

    table: str
    schema: str
    catalog: str | None = None


# ---------------------------------------------------------------------------
# zlib_compress / zlib_decompress
# Ported 1:1 from superset_old/utils/core.py:870-894
# ---------------------------------------------------------------------------


def zlib_compress(data: bytes | str) -> bytes:
    """
    Compress things in a py2/3 safe fashion
    >>> json_str = '{"test": 1}'
    >>> blob = zlib_compress(json_str)
    """
    if isinstance(data, str):
        return zlib.compress(bytes(data, "utf-8"))
    return zlib.compress(data)


def zlib_decompress(blob: bytes, decode: bool | None = True) -> bytes | str:
    """
    Decompress things to a string in a py2/3 safe fashion
    >>> json_str = '{"test": 1}'
    >>> blob = zlib_compress(json_str)
    >>> got_str = zlib_decompress(blob)
    >>> got_str == json_str
    True
    """
    if isinstance(blob, bytes):
        decompressed = zlib.decompress(blob)
    else:
        decompressed = zlib.decompress(bytes(blob, "utf-8"))
    return decompressed.decode("utf-8") if decode else decompressed


# ---------------------------------------------------------------------------
# override_user
# Ported 1:1 from superset_old/utils/core.py:1332-1356, adapted for
# Liteset ContextVar-based user context (no Flask g).
# ---------------------------------------------------------------------------


@contextmanager
def override_user(user: Any, force: bool = True) -> Iterator[Any]:
    """
    Temporarily override the current user for the running async context.

    Sometimes, often in the context of async Celery tasks, it is useful to
    switch the current user (which may be undefined) to a different one,
    execute some SQLAlchemy tasks et al. and then revert back to the original.

    Ported from superset_old/utils/core.py::override_user — adapted for
    Liteset's ContextVar-based user (replaces Flask's ``flask.g.user``).

    :param user: The override user
    :param force: Whether to override the current user if already set
    """
    current = _current_user_ctx.get(None)
    if current is not None and not force:
        # User is already set and force=False — keep existing user
        yield
        return
    token = _current_user_ctx.set(user)
    try:
        yield
    finally:
        _current_user_ctx.reset(token)


# ---------------------------------------------------------------------------
# parse_ssl_cert / create_ssl_cert_file
# Ported 1:1 from superset_old/utils/core.py:1359-1395
# The original used ``app.config["SSL_CERT_PATH"]``; we use SupersetSettings.
# ---------------------------------------------------------------------------


def parse_ssl_cert(certificate: str) -> Any:
    """
    Parses the contents of a certificate and returns a valid certificate object
    if valid.

    :param certificate: Contents of certificate file
    :return: Valid certificate instance
    :raises CertificateException: If certificate is not valid/unparseable
    """
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.x509 import load_pem_x509_certificate

        return load_pem_x509_certificate(certificate.encode("utf-8"), default_backend())
    except ValueError as ex:
        from superset.exceptions import CertificateException

        raise CertificateException("Invalid certificate") from ex


def create_ssl_cert_file(certificate: str) -> str:
    """
    This creates a certificate file that can be used to validate HTTPS
    sessions. A certificate is only written to disk once; on subsequent calls,
    only the path of the existing certificate is returned.

    :param certificate: The contents of the certificate
    :return: The path to the certificate file
    :raises CertificateException: If certificate is not valid/unparseable
    """
    filename = f"{md5_sha_from_str(certificate)}.crt"
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        cert_dir = getattr(settings, "ssl_cert_path", None)
    except Exception:  # noqa: BLE001
        cert_dir = None
    path = cert_dir if cert_dir else tempfile.gettempdir()
    path = os.path.join(path, filename)
    if not os.path.exists(path):
        # Validate certificate prior to persisting to temporary directory
        parse_ssl_cert(certificate)
        with open(path, "w") as cert_file:
            cert_file.write(certificate)
    return path


# ---------------------------------------------------------------------------
# parse_boolean_string
# Ported 1:1 from superset_old/utils/core.py:1825-1851
# ---------------------------------------------------------------------------


def parse_boolean_string(bool_str: str | None) -> bool:
    """
    Convert a string representation of a true/false value into a boolean

    >>> parse_boolean_string(None)
    False
    >>> parse_boolean_string('false')
    False
    >>> parse_boolean_string('true')
    True
    >>> parse_boolean_string('False')
    False
    >>> parse_boolean_string('True')
    True
    >>> parse_boolean_string('foo')
    False
    >>> parse_boolean_string('0')
    False
    >>> parse_boolean_string('1')
    True

    :param bool_str: string representation of a value that is assumed to be boolean
    :return: parsed boolean value
    """
    if bool_str is None:
        return False
    return bool_str.lower() in ("y", "yes", "true", "t", "on", "1")


# ---------------------------------------------------------------------------
# markdown
# Ported 1:1 from superset_old/utils/core.py:478-522
# ---------------------------------------------------------------------------


def markdown(raw: str, markup_wrap: bool | None = False) -> str:
    import markdown as md
    import nh3

    safe_markdown_tags = {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "b",
        "i",
        "strong",
        "em",
        "tt",
        "p",
        "br",
        "span",
        "div",
        "blockquote",
        "code",
        "hr",
        "ul",
        "ol",
        "li",
        "dd",
        "dt",
        "img",
        "a",
    }
    safe_markdown_attrs: dict[str, set[str]] = {
        "img": {"src", "alt", "title"},
        "a": {"href", "alt", "title"},
    }
    safe = md.markdown(
        raw or "",
        extensions=[
            "markdown.extensions.tables",
            "markdown.extensions.fenced_code",
            "markdown.extensions.codehilite",
        ],
    )
    safe = nh3.clean(safe, tags=safe_markdown_tags, attributes=safe_markdown_attrs)
    if markup_wrap:
        try:
            from markupsafe import Markup

            safe = Markup(safe)
        except ImportError:
            pass
    return safe  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# SigalrmTimeout
# Ported 1:1 from superset_old/utils/core.py:598-635
# ---------------------------------------------------------------------------


class SigalrmTimeout:
    """
    To be used in a ``with`` block and timeout its content.
    """

    def __init__(self, seconds: int = 1, error_message: str = "Timeout") -> None:
        self.seconds = seconds
        self.error_message = error_message

    def handle_timeout(  # pylint: disable=unused-argument
        self, signum: int, frame: Any
    ) -> None:
        logger.error("Process timed out", exc_info=True)
        from superset.errors import ErrorLevel, SupersetErrorType
        from superset.exceptions import SupersetTimeoutException

        raise SupersetTimeoutException(
            error_type=SupersetErrorType.BACKEND_TIMEOUT_ERROR,
            message=self.error_message,
            level=ErrorLevel.ERROR,
            extra={"timeout": self.seconds},
        )

    def __enter__(self) -> None:
        try:
            if threading.current_thread() == threading.main_thread():
                signal.signal(signal.SIGALRM, self.handle_timeout)
                signal.alarm(self.seconds)
        except ValueError as ex:
            logger.warning("timeout can't be used in the current context")
            logger.exception(ex)

    def __exit__(  # pylint: disable=redefined-outer-name,redefined-builtin
        self, type: Any, value: Any, traceback: TracebackType
    ) -> None:
        try:
            signal.alarm(0)
        except ValueError as ex:
            logger.warning("timeout can't be used in the current context")
            logger.exception(ex)
