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
"""Unit tests for the thumbnail digest helpers.

Covers the 1:1 port of ``superset_old/thumbnails/digest.py`` —
``_adjust_string_for_executor`` (per-user thumbnails), the RLS-aware
``_adjust_string_with_rls`` (a user who can only see their own rows must not
be served another tenant's cached thumbnail), and the
``_query_dashboard_datasources`` enumeration that feeds the dashboard digest
its RLS contribution.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from superset.tasks.types import ExecutorType
from superset.thumbnails.digest import (
    _adjust_string_for_executor,
    _adjust_string_with_rls,
    _query_dashboard_datasources,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_cm(user: object):
    """Return a zero-arg context manager factory yielding a mock session whose
    ``User`` lookup resolves to ``user`` — patches ``_metadata_sync_session``.
    """

    @contextmanager
    def _cm():
        session = MagicMock()
        session.execute.return_value.scalars.return_value.one_or_none.return_value = (
            user
        )
        yield session

    return _cm


def _rls_datasource(ds_id: int, filters: list[str]) -> MagicMock:
    ds = MagicMock()
    ds.is_rls_supported = True
    ds.id = ds_id
    ds.get_sqla_row_level_filters.return_value = filters
    return ds


# ---------------------------------------------------------------------------
# _adjust_string_for_executor
# ---------------------------------------------------------------------------


def test_adjust_string_for_executor_current_user_appends():
    """CURRENT_USER executor appends the executor id (per-user thumbnail)."""
    out = _adjust_string_for_executor("base", ExecutorType.CURRENT_USER, "alice")
    assert out == "base\nalice"


def test_adjust_string_for_executor_other_unchanged():
    """Non per-user executors do not change the unique string."""
    out = _adjust_string_for_executor("base", ExecutorType.OWNER, "owner-1")
    assert out == "base"


# ---------------------------------------------------------------------------
# _adjust_string_with_rls  (the RLS-isolation property)
# ---------------------------------------------------------------------------


def test_adjust_string_with_rls_no_user_unchanged():
    """With no resolvable user the string is returned untouched."""
    with (
        patch("superset.utils.rls._metadata_sync_session", _session_cm(None)),
        patch("superset.utils.core.get_current_user", return_value=None),
    ):
        out = _adjust_string_with_rls(
            "base", [_rls_datasource(1, ["tenant = 1"])], "ghost"
        )
    assert out == "base"


def test_adjust_string_with_rls_appends_filters():
    """A resolved user with RLS filters folds them into the digest string."""
    with patch("superset.utils.rls._metadata_sync_session", _session_cm(object())):
        out = _adjust_string_with_rls(
            "base", [_rls_datasource(7, ["tenant_id = 1"])], "alice"
        )
    assert out == "base\n7\ttenant_id = 1\n"


def test_adjust_string_with_rls_differentiates_filters():
    """Different RLS filters MUST produce different digest strings.

    This is the cross-tenant isolation guarantee: two users whose RLS
    predicates differ must not share a cached thumbnail.
    """
    with patch("superset.utils.rls._metadata_sync_session", _session_cm(object())):
        out_a = _adjust_string_with_rls(
            "base", [_rls_datasource(7, ["tenant_id = 1"])], "alice"
        )
    with patch("superset.utils.rls._metadata_sync_session", _session_cm(object())):
        out_b = _adjust_string_with_rls(
            "base", [_rls_datasource(7, ["tenant_id = 2"])], "bob"
        )
    assert out_a != out_b


def test_adjust_string_with_rls_skips_non_rls_datasource():
    """A datasource that does not support RLS contributes nothing."""
    ds = MagicMock()
    ds.is_rls_supported = False
    with patch("superset.utils.rls._metadata_sync_session", _session_cm(object())):
        out = _adjust_string_with_rls("base", [ds], "alice")
    assert out == "base"
    ds.get_sqla_row_level_filters.assert_not_called()


# ---------------------------------------------------------------------------
# _query_dashboard_datasources
# ---------------------------------------------------------------------------


def test_query_dashboard_datasources_table_type_only():
    """Only ``table``-type, non-null datasource ids are loaded."""
    session = MagicMock()
    rows_result = MagicMock()
    rows_result.all.return_value = [(1, "table"), (2, "query"), (None, "table")]
    ds_result = MagicMock()
    sentinel_table = object()
    ds_result.scalars.return_value.all.return_value = [sentinel_table]
    session.execute.side_effect = [rows_result, ds_result]

    result = _query_dashboard_datasources(session, dashboard_id=99)

    assert result == [sentinel_table]
    # Two queries: slice rows, then the SqlaTable bulk load.
    assert session.execute.call_count == 2


def test_query_dashboard_datasources_empty_when_no_tables():
    """No table-type slices → empty result and no second (bulk-load) query."""
    session = MagicMock()
    rows_result = MagicMock()
    rows_result.all.return_value = [(2, "query"), (None, "table")]
    session.execute.side_effect = [rows_result]

    result = _query_dashboard_datasources(session, dashboard_id=99)

    assert result == []
    assert session.execute.call_count == 1
