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

import uuid
from typing import Any, cast, TypeVar

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.sql.type_api import Variant

from superset.utils.hashing import md5_sha_from_dict

# ---------------------------------------------------------------------------
# Type aliases (originally from superset.superset_typing / superset.utils.core)
# ---------------------------------------------------------------------------
FormData = dict[str, Any]

T = TypeVar("T")


class AdhocFilterClause(dict):
    """Minimal stand-in for the TypedDict used by filter helpers."""


class QueryObjectFilterClause(dict):
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
            return fk.name

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
def MediumText() -> Variant:  # noqa: N802
    return Text().with_variant(MEDIUMTEXT(), "mysql")


def LongText() -> Variant:  # noqa: N802
    return Text().with_variant(LONGTEXT(), "mysql")


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
        form_data["where"] = " AND ".join(
            [f"({sql})" for sql in sql_where_filters]
        )
        form_data["having"] = " AND ".join(
            [f"({sql})" for sql in sql_having_filters]
        )
        form_data["filters"] = simple_where_filters
