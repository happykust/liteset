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
"""Unit tests for SQLAlchemy event listeners in superset.models._listeners."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from superset.models import _listeners


def _history(deleted):
    h = MagicMock()
    h.has_changes.return_value = bool(deleted)
    h.deleted = deleted
    state = MagicMock()
    state.attrs.database_name.history = h
    return state


def _capture_connection():
    """A fake SA connection that records (sql_text, params) for each execute."""
    calls: list[tuple[str, dict]] = []

    def _execute(stmt, params=None):
        calls.append((str(stmt), params or {}))
        return MagicMock()

    conn = MagicMock()
    conn.execute.side_effect = _execute
    return conn, calls


def test_database_rename_updates_dataset_and_chart_perm_by_prefix():
    """On a database rename, the datasource-access ``perm`` on tables/slices
    must be rewritten by leading-prefix substitution (``[old].`` -> ``[new].``)
    so a dataset perm ``[old].[t](id:5)`` becomes ``[new].[t](id:5)``.

    The previous REPLACE on the database-perm string ``[old].(id:DB_ID)`` was a
    no-op (never a substring of a dataset perm), leaving stored perms stale and
    breaking dataset/chart RBAC after a rename.
    """
    target = MagicMock()
    target.id = 42
    target.database_name = "newdb"

    conn, calls = _capture_connection()

    with patch.object(_listeners.sa, "inspect", return_value=_history(["olddb"])):
        _listeners._database_after_update(MagicMock(), conn, target)

    # Collect the UPDATE statements against tables / slices.
    perm_updates = [
        (sql, params)
        for sql, params in calls
        if ("UPDATE tables" in sql or "UPDATE slices" in sql) and "SET perm" in sql
    ]
    assert len(perm_updates) == 2, f"expected tables+slices perm updates, got {calls}"
    for sql, params in perm_updates:
        # prefix substitution, NOT the old no-op REPLACE
        assert "SUBSTR(perm, 1" in sql
        assert "REPLACE" not in sql
        assert params["old"] == "[olddb]."
        assert params["new"] == "[newdb]."


def test_database_rename_noop_when_name_unchanged():
    """No perm updates when database_name did not change."""
    target = MagicMock()
    target.id = 1
    target.database_name = "samedb"

    conn, calls = _capture_connection()
    with patch.object(_listeners.sa, "inspect", return_value=_history([])):
        _listeners._database_after_update(MagicMock(), conn, target)

    assert calls == []
