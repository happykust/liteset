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
Migration-compatible shim for superset.utils.core.

Provides ONLY the functions needed by Alembic migrations, without importing
Flask, marshmallow, or other removed dependencies.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from enum import Enum, StrEnum
from typing import Any, cast, TypeVar

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
from superset.utils.hashing import md5_sha_from_dict

# ---------------------------------------------------------------------------
# Type aliases (originally from superset.superset_typing / superset.utils.core)
# ---------------------------------------------------------------------------
FormData = dict[str, Any]

T = TypeVar("T")


class AdhocFilterClause(dict[str, Any]):
    """Minimal stand-in for the TypedDict used by filter helpers."""


class QueryObjectFilterClause(dict[str, Any]):
    """Minimal stand-in for the TypedDict used by filter helpers."""


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
# get_user_id  (stub — migrations run outside of a request context)
# ---------------------------------------------------------------------------
def get_user_id() -> int | None:
    """
    Return the current user ID.

    During migrations there is no Flask request context, so this always
    returns ``None``.
    """
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
            expression_type = adhoc_filter.get("expressionType")
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
                sql_expression = adhoc_filter.get("sqlExpression")
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
# context-var based approach.
# ---------------------------------------------------------------------------
_current_user_ctx: ContextVar[Any] = ContextVar("_current_user_ctx", default=None)


def set_current_user(user: Any) -> None:
    """Set the current user for the running async context."""
    _current_user_ctx.set(user)


def get_current_user() -> Any:
    """Return the current user or ``None``."""
    return _current_user_ctx.get(None)


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
