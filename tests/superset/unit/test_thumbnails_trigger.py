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

Covers ``_adjust_string_for_executor`` (per-user thumbnails), the RLS-aware
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


def _session_cm(user: object):
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


def test_adjust_string_for_executor_current_user_appends():
    out = _adjust_string_for_executor("base", ExecutorType.CURRENT_USER, "alice")
    assert out == "base\nalice"


def test_adjust_string_for_executor_other_unchanged():
    out = _adjust_string_for_executor("base", ExecutorType.OWNER, "owner-1")
    assert out == "base"


def test_adjust_string_with_rls_no_user_unchanged():
    with (
        patch("superset.utils.rls._metadata_sync_session", _session_cm(None)),
        patch("superset.utils.core.get_current_user", return_value=None),
    ):
        out = _adjust_string_with_rls(
            "base", [_rls_datasource(1, ["tenant = 1"])], "ghost"
        )
    assert out == "base"


def test_adjust_string_with_rls_appends_filters():
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
    ds = MagicMock()
    ds.is_rls_supported = False
    with patch("superset.utils.rls._metadata_sync_session", _session_cm(object())):
        out = _adjust_string_with_rls("base", [ds], "alice")
    assert out == "base"
    ds.get_sqla_row_level_filters.assert_not_called()


def test_query_dashboard_datasources_table_type_only():
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
    session = MagicMock()
    rows_result = MagicMock()
    rows_result.all.return_value = [(2, "query"), (None, "table")]
    session.execute.side_effect = [rows_result]

    result = _query_dashboard_datasources(session, dashboard_id=99)

    assert result == []
    assert session.execute.call_count == 1


# ---------------------------------------------------------------------------
# R13-02 / R13-03 regression: digest on an async-session-bound ORM object
# whose lazy relationships are NOT eager-loaded must not raise MissingGreenlet.
# Mirrors the list-endpoint serialization path (slices / table.database are
# not preloaded there); before the fix these raised
# ``sqlalchemy.exc.MissingGreenlet`` -> HTTP 500.  The digest reads the lazy
# data through a sync metadata session instead — patched here onto the same
# SQLite file the async fetch uses so the sync lookups resolve.
# ---------------------------------------------------------------------------


class _FakeUser:
    username = "alice"
    id = 1
    is_anonymous = False
    is_authenticated = True


def _patch_metadata_engine(monkeypatch, sync_engine):
    import superset.utils.rls as rls_mod

    monkeypatch.setattr(rls_mod, "_metadata_sync_engine", lambda: sync_engine)


async def test_get_dashboard_digest_no_slices_preload_does_not_raise(
    tmp_path, monkeypatch
):
    """R13-02: get_dashboard_digest must not trip MissingGreenlet when the
    dashboard's ``slices`` relationship is not eager-loaded (the dashboard-list
    query only preloads owners/roles/tags/changed_by/created_by)."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import selectinload, Session as SyncSession

    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice
    from superset.thumbnails.digest import get_dashboard_digest
    from superset.utils.core import set_current_user

    db_file = tmp_path / "r1302.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Dashboard.metadata.create_all(sync_engine)
    with SyncSession(sync_engine) as session:
        dash = Dashboard(dashboard_title="t", slug="r1302")
        dash.slices = [Slice(slice_name="c1", datasource_type="table")]
        session.add(dash)
        session.commit()
        dash_id = dash.id

    _patch_metadata_engine(monkeypatch, sync_engine)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    set_current_user(_FakeUser())
    try:
        async with maker() as session:
            # NB: slices intentionally NOT in options — mirrors the list path.
            dash = (
                (
                    await session.execute(
                        select(Dashboard)
                        .where(Dashboard.id == dash_id)
                        .options(selectinload(Dashboard.owners))
                    )
                )
                .scalars()
                .first()
            )
            digest = get_dashboard_digest(dash)
        assert isinstance(digest, str)
        assert digest
    finally:
        set_current_user(None)
        await engine.dispose()


async def test_get_chart_digest_no_database_preload_does_not_raise(
    tmp_path, monkeypatch
):
    """R13-03: get_chart_digest must not trip MissingGreenlet when the chart's
    ``table.database`` relationship is not eager-loaded (the chart-list query
    preloads only ``Slice.table``); the RLS digest walk reads
    ``datasource.database``."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import selectinload, Session as SyncSession

    from superset.models.connectors import SqlaTable
    from superset.models.core import Database
    from superset.models.security import User
    from superset.models.slice import Slice
    from superset.thumbnails.digest import get_chart_digest
    from superset.utils.core import set_current_user

    db_file = tmp_path / "r1303.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Slice.metadata.create_all(sync_engine)
    with SyncSession(sync_engine) as session:
        # A real ``alice`` row makes the RLS digest walk resolve the executor
        # user and proceed to ``datasource.database`` — the exact attribute
        # that tripped R13-03 on an async-bound table.
        session.add(
            User(username="alice", first_name="a", last_name="l", email="a@x.com")
        )
        db = Database(database_name="d", sqlalchemy_uri="sqlite://")
        session.add(db)
        session.flush()
        table = SqlaTable(table_name="t", database_id=db.id)
        table.is_managed_externally = False
        session.add(table)
        session.flush()
        chart = Slice(
            slice_name="c1",
            datasource_type="table",
            datasource_id=table.id,
            params="{}",
        )
        session.add(chart)
        session.commit()
        chart_id = chart.id

    _patch_metadata_engine(monkeypatch, sync_engine)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    set_current_user(_FakeUser())
    try:
        async with maker() as session:
            # Mirror the chart-list query: owners preloaded (get_executor reads
            # them) and Slice.table preloaded — but table.database is NOT.
            chart = (
                (
                    await session.execute(
                        select(Slice)
                        .where(Slice.id == chart_id)
                        .options(
                            selectinload(Slice.owners),
                            selectinload(Slice.table),
                        )
                    )
                )
                .scalars()
                .first()
            )
            digest = get_chart_digest(chart)
        assert isinstance(digest, str)
        assert digest
    finally:
        set_current_user(None)
        await engine.dispose()
