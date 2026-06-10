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
"""Database utilities — ported 1:1 from ``superset_old/databases/utils.py``."""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.engine.url import URL

from superset.exceptions import CommandInvalidError
from superset.sql.parse import Table


class DatabaseInvalidError(CommandInvalidError):
    """Database parameters are invalid.

    Mirrors ``superset_old/commands/database/exceptions.py::DatabaseInvalidError``
    which inherits ``CommandInvalidError`` (status_code=422) so that unhandled
    escapes are returned as HTTP 422, not 500.
    """


def make_url_safe(raw_url: str | URL) -> URL:
    """Wrapper for SQLAlchemy ``make_url()`` that masks sensitive details in errors."""
    if isinstance(raw_url, str):
        url = raw_url.strip()
        try:
            return make_url(url)
        except Exception as ex:
            raise DatabaseInvalidError() from ex

    return raw_url


def get_foreign_keys_metadata(
    database: Any,
    table: Table,
) -> list[dict[str, Any]]:
    foreign_keys = database.get_foreign_keys(table)
    for fk in foreign_keys:
        fk["column_names"] = fk.pop("constrained_columns")
        fk["type"] = "fk"
    return foreign_keys


def get_indexes_metadata(
    database: Any,
    table: Table,
) -> list[dict[str, Any]]:
    indexes = database.get_indexes(table)
    for idx in indexes:
        idx["type"] = "index"
    return indexes


def get_col_type(col: dict[Any, Any]) -> str:
    try:
        dtype = f"{col['type']}"
    except Exception:  # pylint: disable=broad-except
        # sqla.types.JSON __str__ has a bug, so using __class__.
        dtype = col["type"].__class__.__name__
    return dtype


def get_table_metadata(database: Any, table: Table) -> dict[str, Any]:
    """
    Get table metadata information, including type, pk, fks.
    This function raises SQLAlchemyError when a schema is not found.

    Ported 1:1 from ``superset_old/databases/utils.py:65``.

    :param database: The database model
    :param table: Table instance
    :return: Dict table metadata ready for API response
    """
    keys: list[dict[str, Any]] = []
    columns = database.get_columns(table)
    primary_key = database.get_pk_constraint(table)
    if primary_key and primary_key.get("constrained_columns"):
        primary_key["column_names"] = primary_key.pop("constrained_columns")
        primary_key["type"] = "pk"
        keys += [primary_key]
    foreign_keys = get_foreign_keys_metadata(database, table)
    indexes = get_indexes_metadata(database, table)
    keys += foreign_keys + indexes
    payload_columns: list[dict[str, Any]] = []
    table_comment = database.get_table_comment(table)
    for col in columns:
        dtype = get_col_type(col)
        payload_columns.append(
            {
                "name": col["column_name"],
                "type": dtype.split("(")[0] if "(" in dtype else dtype,
                "longType": dtype,
                "keys": [k for k in keys if col["column_name"] in k["column_names"]],
                "comment": col.get("comment"),
            }
        )
    return {
        "name": table.table,
        "columns": payload_columns,
        "selectStar": database.select_star(
            table,
            indent=True,
            cols=columns,
            latest_partition=True,
        ),
        "primaryKey": primary_key,
        "foreignKeys": foreign_keys,
        "indexes": keys,
        "comment": table_comment,
    }


__all__ = [
    "make_url_safe",
    "DatabaseInvalidError",
    "get_foreign_keys_metadata",
    "get_indexes_metadata",
    "get_col_type",
    "get_table_metadata",
]
