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
"""Regression tests for Query.to_dict() 'user' field type contract.

Original contract (superset_old/models/sql_lab.py:194):
    "user": user_label(self.user)

Where user_label() returns None when user is falsy.  Prior to the fix,
the liteset port defaulted _user_label to "" (empty string) so a query
with no associated user produced ``"user": ""`` instead of ``"user": null``,
breaking API clients that do a strict null check on the field.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch


def _make_query_mock(user_value=None, database_value=None):
    """Build a MagicMock carrying the minimal attributes read by to_dict().

    We call ``Query.to_dict(mock)`` (unbound-style) so that SQLAlchemy
    instrumented attribute assignment is avoided entirely; the mock just
    provides plain attribute reads.
    """
    from superset.models.sql_lab import LimitingFactor, Query

    q = MagicMock(spec=Query)
    q.changed_on = datetime(2024, 1, 1, 12, 0, 0)
    q.database_id = 1
    q.end_time = None
    q.error_message = None
    q.executed_sql = "SELECT 1"
    q.client_id = "abc123"
    q.id = 42
    q.limit = 100
    q.limiting_factor = LimitingFactor.NOT_LIMITED
    q.progress = 0
    q.rows = None
    q.catalog = None
    q.schema = "public"
    q.select_as_cta = False
    q.sql = "SELECT 1"
    q.sql_editor_id = "ed1"
    q.start_time = None
    q.status = "success"
    q.tab_name = "Test Tab"
    q.tmp_schema_name = None
    q.tmp_table_name = None
    q.user_id = None
    q.results_key = None
    q.tracking_url_raw = None
    q.extra = {}
    q.user = user_value
    q.database = database_value
    return q


def _state_with_loaded(*loaded_relationships: str):
    """Return a mock SA InstanceState where specified relationships ARE loaded.

    ``state.unloaded`` is a frozenset of attribute names NOT yet loaded.
    Passing relationship names marks them as loaded (absent from .unloaded).
    """
    state = MagicMock()
    all_rels = {"user", "database"}
    state.unloaded = frozenset(all_rels - set(loaded_relationships))
    return state


# ---------------------------------------------------------------------------
# Core regression: user is None → "user" must be null (None), not ""
# ---------------------------------------------------------------------------


def test_to_dict_user_none_returns_null():
    """When the user relationship is loaded and user is None, 'user' == None.

    Original: user_label(None) returns None.
    Regression: liteset defaulted _user_label="" so the field was "".
    """
    from superset.models.sql_lab import Query

    db_mock = MagicMock()
    db_mock.database_name = "test_db"
    q = _make_query_mock(user_value=None, database_value=db_mock)
    state = _state_with_loaded("user", "database")

    with patch("sqlalchemy.inspect", return_value=state):
        result = Query.to_dict(q)

    assert result["user"] is None, (
        "to_dict() must return None for 'user' when user is None; "
        f"got {result['user']!r} — regression: was returning '' (empty string)"
    )


def test_to_dict_user_set_returns_first_last():
    """When the user is loaded with first+last name, 'user' is 'First Last'."""
    from superset.models.sql_lab import Query

    user_mock = MagicMock()
    user_mock.first_name = "Jane"
    user_mock.last_name = "Doe"
    user_mock.username = "jdoe"
    db_mock = MagicMock()
    db_mock.database_name = "test_db"
    q = _make_query_mock(user_value=user_mock, database_value=db_mock)
    state = _state_with_loaded("user", "database")

    with patch("sqlalchemy.inspect", return_value=state):
        result = Query.to_dict(q)

    assert result["user"] == "Jane Doe", f"Expected 'Jane Doe', got {result['user']!r}"


def test_to_dict_user_set_username_fallback():
    """When the user has no first/last name, fall back to username."""
    from superset.models.sql_lab import Query

    user_mock = MagicMock()
    user_mock.first_name = ""
    user_mock.last_name = ""
    user_mock.username = "jdoe"
    db_mock = MagicMock()
    db_mock.database_name = "test_db"
    q = _make_query_mock(user_value=user_mock, database_value=db_mock)
    state = _state_with_loaded("user", "database")

    with patch("sqlalchemy.inspect", return_value=state):
        result = Query.to_dict(q)

    assert result["user"] == "jdoe", f"Expected 'jdoe', got {result['user']!r}"


def test_to_dict_user_unloaded_returns_null():
    """When the user relationship is NOT loaded (unloaded), 'user' is None.

    Async context: relationship is unloaded — lazy-load is impossible.
    The original returns null for missing user (user_label(None) == None).
    """
    from superset.models.sql_lab import Query

    db_mock = MagicMock()
    db_mock.database_name = "test_db"
    q = _make_query_mock(user_value=None, database_value=db_mock)
    # user is NOT in loaded_relationships → "user" IS in state.unloaded
    state = _state_with_loaded("database")

    with patch("sqlalchemy.inspect", return_value=state):
        result = Query.to_dict(q)

    assert result["user"] is None, (
        "When user relationship is unloaded, 'user' must be None; "
        f"got {result['user']!r}"
    )
