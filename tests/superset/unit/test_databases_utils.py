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
"""Unit tests for superset/databases/utils.py."""

from __future__ import annotations

import pytest
from sqlalchemy.engine.url import make_url

from superset.databases.utils import DatabaseInvalidError, make_url_safe
from superset.exceptions import CommandException, CommandInvalidError, SupersetException

# ---------------------------------------------------------------------------
# DatabaseInvalidError hierarchy
# ---------------------------------------------------------------------------


def test_database_invalid_error_is_command_invalid_error() -> None:
    """DatabaseInvalidError must be a CommandInvalidError subclass (status 422).

    Inherits CommandInvalidError → CommandException → SupersetException.
    Unhandled escapes must be caught by superset_exception_handler at 422,
    not generic_exception_handler at 500.
    """
    exc = DatabaseInvalidError()
    assert isinstance(exc, CommandInvalidError)
    assert isinstance(exc, CommandException)
    assert isinstance(exc, SupersetException)


def test_database_invalid_error_status_code_is_422() -> None:
    """status_code must be 422 so the HTTP response is 422, not 500."""
    exc = DatabaseInvalidError()
    assert exc.status_code == 422


# ---------------------------------------------------------------------------
# make_url_safe raises DatabaseInvalidError on bad input
# ---------------------------------------------------------------------------


def test_make_url_safe_raises_database_invalid_error_on_bad_url() -> None:
    """make_url_safe must raise DatabaseInvalidError for an unparseable URI."""
    with pytest.raises(DatabaseInvalidError):
        make_url_safe("not_a_valid_url")


def test_make_url_safe_valid_string() -> None:
    """make_url_safe returns a URL object for a valid URI string."""
    uri = "postgresql+psycopg2://user:pass@localhost:5432/db"
    result = make_url_safe(uri)
    assert result == make_url(uri)


def test_make_url_safe_url_passthrough() -> None:
    """make_url_safe returns the URL object unchanged when passed a URL."""
    url = make_url("postgresql+psycopg2://user:pass@localhost:5432/db")
    assert make_url_safe(url) is url


# ---------------------------------------------------------------------------
# get_table_metadata — requires the five Database introspection wrappers;
# without them every /database/{pk}/table_metadata/ call dies with AttributeError → 422.
# ---------------------------------------------------------------------------


def test_database_model_has_introspection_methods() -> None:
    from superset.models.core import Database

    for method in (
        "get_columns",
        "get_pk_constraint",
        "get_foreign_keys",
        "get_indexes",
        "get_table_comment",
        "select_star",
    ):
        assert callable(getattr(Database, method, None)), (
            f"Database.{method} is required by get_table_metadata"
        )


def test_get_table_metadata_payload_shape() -> None:
    from unittest.mock import MagicMock

    from superset.databases.utils import get_table_metadata
    from superset.sql.parse import Table

    database = MagicMock()
    database.get_columns.return_value = [
        {"column_name": "id", "type": "INTEGER(11)", "comment": None},
        {"column_name": "name", "type": "VARCHAR(255)", "comment": "label"},
    ]
    database.get_pk_constraint.return_value = {
        "constrained_columns": ["id"],
        "name": "pk_t",
    }
    database.get_foreign_keys.return_value = []
    database.get_indexes.return_value = []
    database.get_table_comment.return_value = "a table"
    database.select_star.return_value = "SELECT *\nFROM t"

    payload = get_table_metadata(database, Table("t", "public"))

    assert payload["name"] == "t"
    assert payload["comment"] == "a table"
    assert payload["selectStar"] == "SELECT *\nFROM t"
    assert payload["primaryKey"]["column_names"] == ["id"]
    assert payload["primaryKey"]["type"] == "pk"
    cols = {c["name"]: c for c in payload["columns"]}
    assert cols["id"]["type"] == "INTEGER"
    assert cols["id"]["longType"] == "INTEGER(11)"
    assert cols["id"]["keys"], "pk must be attached to the id column"
    assert cols["name"]["comment"] == "label"
