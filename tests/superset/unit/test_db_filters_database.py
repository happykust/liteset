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
"""Unit tests for database-access filter helpers in ``superset/db/filters.py``.

Mirrors the original ``superset_old/databases/filters.py::DatabaseFilter``:
  * ``_databases_from_view_menus`` extracts DB names from view-menu strings.
  * ``_apply_extra_dynamic_database_filters`` adapts the
    ``EXTRA_DYNAMIC_QUERY_FILTERS["databases"]`` callable for async use.
  * ``database_access_filters`` applies dynamic clauses BEFORE the RBAC check
    so that even all-DB-access users receive any configured dynamic restriction
    (original lines 52-59 of DatabaseFilter.apply).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.db.filters import (
    _apply_extra_dynamic_database_filters,
    _databases_from_view_menus,
    database_access_filters,
)
from superset.models.core import Database

# ---------------------------------------------------------------------------
# _databases_from_view_menus
# ---------------------------------------------------------------------------


def test_databases_from_view_menus_datasource_access() -> None:
    """Extracts DB names from datasource_access-style view menu strings.

    Mirrors ``superset_old/databases/filters.py::can_access_databases`` which
    does ``vm.split(".")[0][1:-1]`` over the raw view-menu name set.
    """
    names = _databases_from_view_menus(
        {
            "[my_db].[examples].[public].[table1](id:1)",
            "[my_other_db].[examples].[public].[table1](id:2)",
        }
    )
    assert names == {"my_db", "my_other_db"}


def test_databases_from_view_menus_schema_access() -> None:
    """Extracts DB names from schema_access-style view menu strings."""
    names = _databases_from_view_menus(
        {
            "[my_db].[examples].[information_schema]",
            "[my_db].[other].[secret]",
            "[third_db].[schema]",
        }
    )
    assert names == {"my_db", "third_db"}


def test_databases_from_view_menus_catalog_access() -> None:
    """Extracts DB names from catalog_access-style view menu strings."""
    names = _databases_from_view_menus(
        {
            "[my_db].[examples]",
            "[my_db].[other]",
        }
    )
    assert names == {"my_db"}


def test_databases_from_view_menus_empty_set() -> None:
    """Empty input returns an empty set."""
    assert _databases_from_view_menus(set()) == set()


def test_databases_from_view_menus_malformed_skipped() -> None:
    """Entries too short to have brackets are skipped rather than raising."""
    # Single-character head would produce empty string after [1:-1]; guard
    # ensures no crash and the malformed entry is silently discarded.
    names = _databases_from_view_menus({"x", "", "[good].[schema]"})
    assert names == {"good"}


# ---------------------------------------------------------------------------
# _apply_extra_dynamic_database_filters
# ---------------------------------------------------------------------------


def _mock_settings(extra_dynamic_query_filters: dict) -> MagicMock:
    """Return a mock SupersetSettings with extra_dynamic_query_filters set."""
    s = MagicMock()
    s.extra_dynamic_query_filters = extra_dynamic_query_filters
    return s


def _patch_sync_session():
    """Patch the thread-local sync session factory with a detached mock.

    ``_apply_extra_dynamic_database_filters`` only needs a Session object to
    bind the detached ``Query``; building a real engine from the (mocked)
    settings would crash in ``create_engine`` on a cold cache.
    """
    return patch("superset.db.session.get_sync_session", return_value=MagicMock())


def test_apply_extra_dynamic_no_config() -> None:
    """Returns ``[]`` when ``EXTRA_DYNAMIC_QUERY_FILTERS`` is falsy."""
    with (
        _patch_sync_session(),
        patch("superset.config.SupersetSettings", return_value=_mock_settings({})),
    ):
        assert _apply_extra_dynamic_database_filters() == []


def test_apply_extra_dynamic_no_databases_key() -> None:
    """Returns ``[]`` when the config dict has no ``"databases"`` key."""
    with (
        _patch_sync_session(),
        patch(
            "superset.config.SupersetSettings",
            return_value=_mock_settings({"charts": lambda q: q}),
        ),
    ):
        assert _apply_extra_dynamic_database_filters() == []


def test_apply_extra_dynamic_filter_only_callable() -> None:
    """A callable that only adds a WHERE clause returns that WHERE expression.

    The original ``DatabaseFilter.apply`` passes the FAB query directly to the
    callable; we construct a detached LegacyQuery, apply the callable, and
    extract the WHERE clause from the resulting statement so it can be merged
    into the async filter list.
    """

    def only_id_gt_10(query):  # type: ignore[no-untyped-def]
        return query.filter(Database.id > 10)

    with (
        _patch_sync_session(),
        patch(
            "superset.config.SupersetSettings",
            return_value=_mock_settings({"databases": only_id_gt_10}),
        ),
    ):
        clauses = _apply_extra_dynamic_database_filters()

    assert len(clauses) == 1
    # The returned clause must represent "dbs.id > :id_1" (bind-param form).
    clause_str = str(clauses[0])
    assert "dbs.id" in clause_str
    assert ">" in clause_str


def test_apply_extra_dynamic_no_op_callable_returns_empty() -> None:
    """A callable that adds no filter returns ``[]`` (no spurious restriction)."""

    def no_op(query):  # type: ignore[no-untyped-def]
        return query

    with (
        _patch_sync_session(),
        patch(
            "superset.config.SupersetSettings",
            return_value=_mock_settings({"databases": no_op}),
        ),
    ):
        clauses = _apply_extra_dynamic_database_filters()

    assert clauses == []


def test_apply_extra_dynamic_join_callable_returns_in_subquery() -> None:
    """A callable that adds a JOIN is translated to a Database.id IN (...) clause.

    When the callable introduces a JOIN (e.g. to filter by a related table),
    the full filtered statement is wrapped in a scalar sub-select on
    ``Database.id`` so the JOIN semantics are preserved.
    """
    from superset.models.connectors import SqlaTable

    def join_filter(query):  # type: ignore[no-untyped-def]
        return query.join(SqlaTable, SqlaTable.database_id == Database.id).filter(
            SqlaTable.schema == "allowed_schema"
        )

    with (
        _patch_sync_session(),
        patch(
            "superset.config.SupersetSettings",
            return_value=_mock_settings({"databases": join_filter}),
        ),
    ):
        clauses = _apply_extra_dynamic_database_filters()

    assert len(clauses) == 1
    clause_str = str(clauses[0])
    # The clause must be an IN expression scoped to the Database primary key.
    assert "dbs.id IN" in clause_str or "dbs.id in" in clause_str.lower()
    # The JOIN table must appear in the sub-select SQL.
    assert "tables" in clause_str
    # "schema" column name must appear (value is a bind parameter).
    assert "schema" in clause_str


# ---------------------------------------------------------------------------
# database_access_filters
# ---------------------------------------------------------------------------


def _make_security_manager(
    *,
    can_access_all_databases: bool,
    database_access_perms: set[str] | None = None,
    catalog_access_perms: set[str] | None = None,
    schema_access_perms: set[str] | None = None,
    datasource_access_perms: set[str] | None = None,
) -> MagicMock:
    """Build a minimal mock security manager for database_access_filters tests."""
    sm = MagicMock()
    sm.can_access_all_databases = AsyncMock(return_value=can_access_all_databases)

    async def _view_menu_names(name: str, *, user: object) -> set[str]:
        mapping = {
            "database_access": database_access_perms or set(),
            "catalog_access": catalog_access_perms or set(),
            "schema_access": schema_access_perms or set(),
            "datasource_access": datasource_access_perms or set(),
        }
        return mapping.get(name, set())

    sm.user_view_menu_names = _view_menu_names
    return sm


@pytest.mark.asyncio
async def test_database_access_filters_all_db_access_no_dynamic() -> None:
    """User with all_database_access and no dynamic filter → empty list.

    Original: ``can_access_all_databases()`` returns query unmodified
    (``return query``).  Liteset returns ``dynamic_clauses`` which is ``[]``
    when no EXTRA_DYNAMIC_QUERY_FILTERS is configured.
    """
    sm = _make_security_manager(can_access_all_databases=True)
    user = MagicMock()

    with patch(
        "superset.db.filters._apply_extra_dynamic_database_filters",
        return_value=[],
    ):
        result = await database_access_filters(sm, user)

    assert result == []
    sm.can_access_all_databases.assert_awaited_once_with(user=user)


@pytest.mark.asyncio
async def test_database_access_filters_all_db_access_with_dynamic() -> None:
    """Dynamic clauses ARE returned even for all_database_access users.

    Original line 59: ``return query`` after ``query = dynamic_filter(query)``
    means the dynamic filter IS applied to the full-access path.  The liteset
    mirrors this: ``return dynamic_clauses`` (not ``return []``) when the user
    has all_database_access.
    """
    sm = _make_security_manager(can_access_all_databases=True)
    user = MagicMock()

    sentinel_clause = Database.id > 999

    with patch(
        "superset.db.filters._apply_extra_dynamic_database_filters",
        return_value=[sentinel_clause],
    ):
        result = await database_access_filters(sm, user)

    # The dynamic clause must be present even for the all-access user.
    assert len(result) == 1
    assert str(result[0]) == str(sentinel_clause)


@pytest.mark.asyncio
async def test_database_access_filters_restricted_user_rbac_clauses() -> None:
    """Restricted user gets dynamic clauses PLUS the RBAC OR clause.

    Mirrors ``superset_old/databases/filters.py:DatabaseFilter.apply`` lines
    61-75: ``database_perms`` and ``database_names`` derived from view-menu
    names are combined into a single OR and appended after any dynamic clauses.
    """
    sm = _make_security_manager(
        can_access_all_databases=False,
        database_access_perms={"[prod].(id:1)"},
        schema_access_perms={"[staging].[public]"},
    )
    user = MagicMock()

    with patch(
        "superset.db.filters._apply_extra_dynamic_database_filters",
        return_value=[],
    ):
        result = await database_access_filters(sm, user)

    # One RBAC clause (OR of perm.in_ / database_name.in_).
    assert len(result) == 1
    clause_str = str(result[0])
    # The clause must reference the database_name column (both branches use it).
    assert "database_name" in clause_str


@pytest.mark.asyncio
async def test_database_access_filters_dynamic_prepended_before_rbac() -> None:
    """Dynamic clauses appear BEFORE the RBAC clause in the returned list.

    Original ordering (``DatabaseFilter.apply``): dynamic filter first, then
    RBAC check.  In the async port: ``dynamic_clauses + [rbac_clause]``.
    """
    sm = _make_security_manager(
        can_access_all_databases=False,
        database_access_perms={"[prod].(id:1)"},
    )
    user = MagicMock()

    sentinel_clause = Database.id > 5

    with patch(
        "superset.db.filters._apply_extra_dynamic_database_filters",
        return_value=[sentinel_clause],
    ):
        result = await database_access_filters(sm, user)

    # Dynamic clause at index 0, RBAC OR clause at index 1.
    assert len(result) == 2
    assert str(result[0]) == str(sentinel_clause)
    # The second clause is the RBAC OR (references database_name column).
    rbac_str = str(result[1])
    assert "database_name" in rbac_str
