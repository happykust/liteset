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
from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from superset.commands.chart.create import CreateChartCommand
from superset.commands.chart.delete import (
    BulkDeleteChartsCommand,
    DeleteChartCommand,
)
from superset.commands.chart.export import ExportChartsCommand
from superset.commands.chart.update import UpdateChartCommand
from superset.commands.chart.warm_up_cache import WarmUpChartCacheCommand
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetSecurityException,
)


def _security_exception(msg: str = "denied") -> SupersetSecurityException:
    """Build a SupersetSecurityException the production way (SupersetError
    positional, not ``message=``)."""
    return SupersetSecurityException(
        SupersetError(
            error_type=SupersetErrorType.MISSING_OWNERSHIP_ERROR,
            message=msg,
            level=ErrorLevel.ERROR,
        )
    )


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
    # Owner-tagging / lookups issue ``(await session.execute()).scalars()
    # .unique().one_or_none()`` / ``.all()`` — all SYNC on the awaited result.
    # A bare AsyncMock makes ``.scalars()`` a coroutine; configure concrete
    # results so those chains don't crash the create/delete tests.
    _res = MagicMock()
    _res.scalars.return_value.unique.return_value.one_or_none.return_value = None
    _res.scalars.return_value.unique.return_value.all.return_value = []
    _res.scalars.return_value.one_or_none.return_value = None
    _res.scalars.return_value.all.return_value = []
    dao.session.execute = AsyncMock(return_value=_res)
    dao.session.begin_nested = MagicMock(return_value=AsyncMock())
    return dao


def _exec_returns(mock_dao, *, unique_one=None, one=None, all_=None):
    """Make ``session.execute`` resolve to a concrete result. Commands load via
    ``(await session.execute(stmt)).scalars().unique().one_or_none()`` (chart),
    ``.scalars().one_or_none()`` (export), or ``.scalars().all()`` (report
    schedules) — not the ``find_by_id`` the older tests mocked."""
    res = MagicMock()
    res.scalars.return_value.unique.return_value.one_or_none.return_value = unique_one
    res.scalars.return_value.unique.return_value.all.return_value = all_ or []
    res.scalars.return_value.one_or_none.return_value = (
        one if one is not None else unique_one
    )
    res.scalars.return_value.all.return_value = all_ or []
    mock_dao.session.execute = AsyncMock(return_value=res)


@pytest.fixture
def mock_chart():
    chart = MagicMock()
    chart.id = 1
    chart.slice_name = "Test Chart"
    chart.viz_type = "table"
    chart.params = "{}"
    chart.query_context = None
    chart.cache_timeout = None
    chart.uuid = None
    chart.datasource_id = 1
    chart.datasource_type = "table"
    chart.datasource = None  # No datasource object for export bundling
    # ``export._export_single`` and ``warm_up`` read ``chart.table`` (the
    # SqlaTable relationship); a bare MagicMock attribute would be truthy and
    # send export down the dataset-bundling path (secure_filename(MagicMock)
    # -> normalize() TypeError). Explicitly clear it.
    chart.table = None
    return chart


async def test_create_chart_validates_slice_name(mock_dao):
    cmd = CreateChartCommand(dao=mock_dao, data={"viz_type": "table"})
    with pytest.raises(CommandInvalidError, match="slice_name"):
        await cmd.validate()


async def test_create_chart_validates_success(mock_dao):
    cmd = CreateChartCommand(
        dao=mock_dao,
        data={"slice_name": "Test", "viz_type": "table"},
        user_id=1,
    )
    await cmd.validate()  # Should not raise


async def test_update_chart_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateChartCommand(dao=mock_dao, chart_id=999, data={"slice_name": "X"})
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_chart_success(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    _exec_returns(mock_dao, unique_one=mock_chart)
    cmd = UpdateChartCommand(
        dao=mock_dao,
        chart_id=1,
        data={"slice_name": "Updated"},
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.slice_name == "Updated"
    # ``run()`` flushes once for the chart and again inside
    # ``sync_owner_tags_after_update`` — assert it was awaited (not once).
    mock_dao.session.flush.assert_awaited()


async def test_update_chart_normal_save_bumps_last_saved(mock_dao, mock_chart):
    """A normal user save (no ``query_context_generation``) bumps both
    ``last_saved_by_fk`` and ``last_saved_at`` — 1:1 with upstream
    ``UpdateChartCommand.run`` (superset_old/commands/chart/update.py:69-71)."""
    mock_chart.last_saved_by_fk = None
    mock_chart.last_saved_at = None
    _exec_returns(mock_dao, unique_one=mock_chart)
    cmd = UpdateChartCommand(
        dao=mock_dao,
        chart_id=1,
        data={"slice_name": "Updated"},
        user_id=42,
    )
    await cmd.validate()
    await cmd.run()
    assert mock_chart.last_saved_by_fk == 42
    assert mock_chart.last_saved_at is not None


async def test_update_chart_query_context_regeneration_skips_last_saved(
    mock_dao, mock_chart
):
    """A background report/cache worker regenerating the stored
    ``query_context`` must NOT touch ``last_saved_*`` — upstream gates both
    assignments on ``query_context_generation`` being falsy
    (superset_old/commands/chart/update.py:69-71)."""
    mock_chart.last_saved_by_fk = None
    mock_chart.last_saved_at = None
    _exec_returns(mock_dao, unique_one=mock_chart)
    cmd = UpdateChartCommand(
        dao=mock_dao,
        chart_id=1,
        data={"query_context": "{}", "query_context_generation": True},
        user_id=42,
    )
    await cmd.validate()
    await cmd.run()
    # ``last_saved_*`` untouched; ``changed_by_fk`` still tracks the actor.
    assert mock_chart.last_saved_by_fk is None
    assert mock_chart.last_saved_at is None
    assert mock_chart.changed_by_fk == 42


async def test_update_chart_query_context_regeneration_keeps_owners(
    mock_dao, mock_chart
):
    """A query-context-only update skips the ownership check so report
    workers and non-owner viewers can refresh the stored ``query_context``
    — but it must NOT recompute owners.  Upstream gates ``compute_owners``
    on ``not is_query_context_update(...)``
    (superset_old/commands/chart/update.py:115-128); recomputing here would
    prepend the non-admin actor to ``owners`` (compute_owner_list auto-adds
    a non-admin caller missing from the list) — silent ownership escalation."""
    existing_owner = MagicMock()
    existing_owner.id = 7
    mock_chart.owners = [existing_owner]
    _exec_returns(mock_dao, unique_one=mock_chart)

    actor = MagicMock()
    actor.id = 42
    users = {42: actor, 7: existing_owner}
    sm = AsyncMock()
    sm.is_admin = MagicMock(return_value=False)
    sm.find_user_by_id = AsyncMock(side_effect=lambda uid: users.get(uid))

    cmd = UpdateChartCommand(
        dao=mock_dao,
        chart_id=1,
        data={"query_context": "{}", "query_context_generation": True},
        user_id=42,
        security_manager=sm,
    )
    await cmd.validate()
    await cmd.run()
    # Ownership check skipped (that's the point of the qcu path)…
    sm.raise_for_ownership.assert_not_awaited()
    # …but owners must stay exactly as they were: no actor prepended.
    assert [o.id for o in mock_chart.owners] == [7]


async def test_delete_chart_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteChartCommand(dao=mock_dao, chart_id=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_chart_invisible_is_404(mock_dao, mock_chart):
    """A chart outside the caller's visibility scope reads as nonexistent
    (404), not forbidden (403) — upstream ``ChartDAO.find_by_ids`` applies
    ``ChartFilter`` (base_filter) so invisible ids never reach the ownership
    check (superset_old/daos/chart.py:40)."""
    import sqlalchemy as sa

    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    _exec_returns(mock_dao, all_=[])
    sm = AsyncMock()
    sm.find_user_by_id = AsyncMock(return_value=MagicMock(id=42))
    sm.raise_for_ownership = AsyncMock(side_effect=_security_exception())
    with patch(
        "superset.db.filters.chart_access_filters",
        new=AsyncMock(return_value=[sa.text("1=1")]),
    ):
        cmd = DeleteChartCommand(
            dao=mock_dao, chart_id=1, security_manager=sm, user_id=42
        )
        with pytest.raises(ObjectNotFoundError):
            await cmd.validate()
    sm.raise_for_ownership.assert_not_awaited()


async def test_update_chart_attach_unpublished_dashboard_is_not_found(mock_dao, mock_chart):
    """Attaching a chart to a NEW dashboard outside the caller's list-filter
    scope (e.g. an unpublished dashboard they don't own) is the 422
    DashboardsNotFoundValidationError upstream emits — upstream
    ``_validate_new_dashboard_access`` resolves new ids via the FILTERED
    ``DashboardDAO.find_by_ids`` (DashboardAccessFilter, published required),
    NOT the laxer direct-access ``raise_for_dashboard_access`` semantics.
    A 403 (or silent allow) would diverge from upstream curation."""
    import sqlalchemy as sa

    from superset.exceptions import DashboardsNotFoundValidationError

    mock_chart.dashboards = []
    _exec_returns(mock_dao, unique_one=mock_chart, all_=[])
    dash = MagicMock()
    dash.id = 7
    mock_dao.find_dashboards_by_ids = AsyncMock(return_value=[dash])
    sm = AsyncMock()
    sm.find_user_by_id = AsyncMock(return_value=MagicMock(id=42))
    # Direct-access would PASS (lax, no published req) — the bug used this.
    sm.can_access_dashboard = AsyncMock(return_value=True)
    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[sa.text("1=1")]),
    ):
        cmd = UpdateChartCommand(
            dao=mock_dao,
            chart_id=1,
            data={"dashboards": [7]},
            user_id=42,
            security_manager=sm,
        )
        with pytest.raises(DashboardsNotFoundValidationError):
            await cmd.validate()


async def test_create_chart_invisible_dashboard_is_not_found(mock_dao):
    """Attaching a new chart to a dashboard the user can't SEE is the 422
    DashboardsNotFoundValidationError upstream emits (filtered
    ``DashboardDAO.find_by_ids`` → count mismatch,
    superset_old/commands/chart/create.py:68-70) — not a 403 that would
    disclose the dashboard's existence."""
    import sqlalchemy as sa

    from superset.exceptions import DashboardsNotFoundValidationError

    dash = MagicMock()
    dash.id = 7
    mock_dao.find_dashboards_by_ids = AsyncMock(return_value=[dash])
    _exec_returns(mock_dao, all_=[])
    sm = AsyncMock()
    sm.find_user_by_id = AsyncMock(return_value=MagicMock(id=42))
    sm.raise_for_ownership = AsyncMock(side_effect=_security_exception())
    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[sa.text("1=1")]),
    ):
        cmd = CreateChartCommand(
            dao=mock_dao,
            data={"slice_name": "Test", "dashboards": [7]},
            user_id=42,
            security_manager=sm,
        )
        with pytest.raises(DashboardsNotFoundValidationError):
            await cmd.validate()
    sm.raise_for_ownership.assert_not_awaited()


async def test_delete_chart_success(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    mock_dao.find_report_schedules_by_chart_id = AsyncMock(return_value=[])
    cmd = DeleteChartCommand(dao=mock_dao, chart_id=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_chart])


async def test_bulk_delete_empty_ids(mock_dao):
    cmd = BulkDeleteChartsCommand(dao=mock_dao, chart_ids=[])
    with pytest.raises(CommandInvalidError, match="No chart IDs"):
        await cmd.validate()


async def test_bulk_delete_success(mock_dao, mock_chart):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_chart])
    cmd = BulkDeleteChartsCommand(dao=mock_dao, chart_ids=[1])
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_chart])


async def test_export_charts_produces_zip(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    # Export loads via session.execute().scalars().one_or_none() and builds the
    # payload from export_to_dict (not field reads).
    mock_chart.export_to_dict.return_value = {
        "slice_name": "Test Chart",
        "viz_type": "table",
    }
    _exec_returns(mock_dao, one=mock_chart)
    # Access gate: count() must equal len(model_ids); filter patched so no
    # real security_manager is needed.
    mock_dao.count = AsyncMock(return_value=1)
    with patch(
        "superset.db.filters.chart_access_filters",
        AsyncMock(return_value=[]),
    ):
        cmd = ExportChartsCommand(model_ids=[1], dao=mock_dao)
        buf = await cmd.execute()
    assert isinstance(buf, io.BytesIO)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        assert any("charts/" in n for n in names)
        assert "metadata.yaml" in names
        # Verify YAML content contains known fields
        chart_files = [n for n in names if n.startswith("charts/")]
        content = yaml.safe_load(zf.read(chart_files[0]))
        assert content["slice_name"] == "Test Chart"
        assert content["viz_type"] == "table"


async def test_warm_up_cache(mock_dao, mock_chart):
    # ``run()`` returns a single ``{chart_id, viz_error, viz_status}`` dict
    # (not a list). The mock chart has no query_context, so the non-legacy
    # branch raises ``CommandInvalidError`` which the run() try-boundary
    # catches into ``viz_error`` with ``viz_status`` left ``None`` — exactly
    # the original's error envelope.
    _exec_returns(mock_dao, one=mock_chart)
    cmd = WarmUpChartCacheCommand(dao=mock_dao, chart_id=1)
    result = await cmd.execute()
    assert result["chart_id"] == 1
    assert result["viz_status"] is None
    assert "query context" in result["viz_error"]


# ---------------------------------------------------------------------------
# Ownership checks
# ---------------------------------------------------------------------------


async def test_delete_non_owner_raises_forbidden(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=_security_exception("You don't have permission")
    )
    cmd = DeleteChartCommand(dao=mock_dao, chart_id=1, security_manager=sm, user_id=42)
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


async def test_update_non_owner_raises_forbidden(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    _exec_returns(mock_dao, unique_one=mock_chart)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=_security_exception("You don't have permission")
    )
    cmd = UpdateChartCommand(
        dao=mock_dao,
        chart_id=1,
        data={"slice_name": "Updated"},
        user_id=42,
        security_manager=sm,
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


async def test_bulk_delete_non_owner_raises_forbidden(mock_dao, mock_chart):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_chart])
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=_security_exception("You don't have permission")
    )
    cmd = BulkDeleteChartsCommand(
        dao=mock_dao, chart_ids=[1], security_manager=sm, user_id=42
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T2: Report schedule guard for DeleteChartCommand
# ---------------------------------------------------------------------------


async def test_delete_chart_with_report_schedules_raises(mock_dao, mock_chart):
    """DeleteChartCommand blocks deletion when report schedules exist."""
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    report = MagicMock()
    report.name = "Weekly Report"
    # The guard uses AsyncReportScheduleDAO(session).find_by_chart_ids, which
    # queries via session.execute().scalars().all() — return a report there.
    _exec_returns(mock_dao, unique_one=mock_chart, all_=[report])
    cmd = DeleteChartCommand(dao=mock_dao, chart_id=1)
    # Raises ``ChartDeleteFailedReportsExistError`` (a ``CommandInvalidError``
    # subclass) with the offending report names in the message.
    with pytest.raises(CommandInvalidError, match="associated alerts or reports"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T3: CreateChartCommand.run() owner resolution branches
# ---------------------------------------------------------------------------


async def test_create_chart_run_explicit_owners(mock_dao):
    """run() resolves explicit owner IDs via security_manager."""
    sm = AsyncMock()
    owner_obj = MagicMock()
    owner_obj.id = 10
    sm.find_user_by_id = AsyncMock(return_value=owner_obj)

    cmd = CreateChartCommand(
        dao=mock_dao,
        data={"slice_name": "Chart A", "viz_type": "table", "owners": [10]},
        user_id=1,
        security_manager=sm,
    )
    await cmd.validate()

    mock_slice_cls = MagicMock()
    instance = MagicMock()
    instance.owners = []
    mock_slice_cls.return_value = instance
    import sys
    from unittest.mock import patch

    with patch.dict(
        sys.modules, {"superset.models.slice": MagicMock(Slice=mock_slice_cls)}
    ):
        result = await cmd.run()

    sm.find_user_by_id.assert_awaited_with(10)
    assert result.owners == [owner_obj]


async def test_create_chart_run_auto_assign_current_user(mock_dao):
    """run() auto-assigns current user as owner when no explicit owners."""
    sm = AsyncMock()
    user_obj = MagicMock()
    user_obj.id = 5
    sm.find_user_by_id = AsyncMock(return_value=user_obj)

    cmd = CreateChartCommand(
        dao=mock_dao,
        data={"slice_name": "Chart B", "viz_type": "bar"},
        user_id=5,
        security_manager=sm,
    )
    await cmd.validate()

    mock_slice_cls = MagicMock()
    instance = MagicMock()
    instance.owners = []
    mock_slice_cls.return_value = instance
    import sys
    from unittest.mock import patch

    with patch.dict(
        sys.modules, {"superset.models.slice": MagicMock(Slice=mock_slice_cls)}
    ):
        result = await cmd.run()

    sm.find_user_by_id.assert_awaited_with(5)
    assert result.owners == [user_obj]


async def test_create_chart_run_user_not_found(mock_dao):
    """run() handles user not found during auto-assign (owners stays empty)."""
    sm = AsyncMock()
    sm.find_user_by_id = AsyncMock(return_value=None)

    cmd = CreateChartCommand(
        dao=mock_dao,
        data={"slice_name": "Chart C", "viz_type": "line"},
        user_id=99,
        security_manager=sm,
    )
    await cmd.validate()

    mock_slice_cls = MagicMock()
    instance = MagicMock()
    instance.owners = []
    mock_slice_cls.return_value = instance
    import sys
    from unittest.mock import patch

    with patch.dict(
        sys.modules, {"superset.models.slice": MagicMock(Slice=mock_slice_cls)}
    ):
        result = await cmd.run()

    sm.find_user_by_id.assert_awaited_with(99)
    # owners should not have been set (no user found)
    assert result.owners == []


async def test_export_charts_denies_inaccessible_id(mock_dao):
    """ExportChartsCommand.validate() raises ObjectNotFoundError when the
    access-control gate returns fewer rows than the requested ids — i.e. the
    caller has no permission to at least one of the requested charts."""
    # count() returns 0 → none of the requested ids are accessible.
    mock_dao.count = AsyncMock(return_value=0)
    with patch(
        "superset.db.filters.chart_access_filters",
        AsyncMock(return_value=[]),
    ):
        cmd = ExportChartsCommand(
            model_ids=[1], dao=mock_dao, security_manager=MagicMock()
        )
        with pytest.raises(ObjectNotFoundError):
            await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T4: Database YAML ``extra`` fixups in chart export
# These mirror superset_old/commands/database/export.py:80-88 (parse_extra +
# schemas_allowed_for_file_upload rename).  The chart export builds the DB
# YAML inline, so it must apply the same two V1-schema-compat fixups.
# ---------------------------------------------------------------------------


async def test_export_chart_db_extra_single_decodes_only(mock_dao):
    """Chart export must decode the database ``extra`` JSON exactly ONCE.

    1:1 with the original: ``ExportChartsCommand._export`` delegates the
    database YAML to ``ExportDatasetsCommand``
    (superset_old/commands/chart/export.py:104), whose ``_export`` only does
    ``payload["extra"] = json.loads(payload["extra"])``
    (superset_old/commands/dataset/export.py:108-112). The
    ``parse_extra()`` double-decode of ``schemas_allowed_for_csv_upload``
    belongs ONLY to ``ExportDatabasesCommand``
    (superset_old/commands/database/export.py:44-51) and must NOT happen here.
    """
    import json as _json
    import zipfile

    import yaml

    chart = MagicMock()
    chart.id = 1
    chart.slice_name = "My Chart"
    chart.tags = []
    chart.export_to_dict.return_value = {"slice_name": "My Chart", "viz_type": "bar"}

    db = MagicMock()
    db.id = 10
    db.database_name = "MyDB"
    db.uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    # extra contains schemas_allowed_for_csv_upload as a JSON-encoded string
    db.export_to_dict.return_value = {
        "database_name": "MyDB",
        "sqlalchemy_uri": "postgresql://x",
        "extra": _json.dumps(
            {"schemas_allowed_for_csv_upload": _json.dumps(["schema1", "schema2"])}
        ),
    }

    dataset = MagicMock()
    dataset.id = 5
    dataset.table_name = "my_table"
    dataset.uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    dataset.database = db
    dataset.export_to_dict.return_value = {
        "table_name": "my_table",
        "sql": None,
    }

    chart.table = dataset

    _exec_returns(mock_dao, one=chart)
    mock_dao.count = AsyncMock(return_value=1)

    # Suppress SSH tunnel lookup
    with (
        patch("superset.db.daos.database.AsyncSSHTunnelDAO", side_effect=ImportError),
        patch(
            "superset.db.filters.chart_access_filters",
            AsyncMock(return_value=[]),
        ),
        patch("superset.utils.feature_flags.feature_flag_manager") as ff,
    ):
        ff.is_feature_enabled.return_value = False
        cmd = ExportChartsCommand(model_ids=[1], dao=mock_dao)
        buf = await cmd.execute()

    with zipfile.ZipFile(buf) as zf:
        db_files = [n for n in zf.namelist() if n.startswith("databases/")]
        assert db_files, "no databases/*.yaml emitted"
        db_yaml = yaml.safe_load(zf.read(db_files[0]))

    extra = db_yaml.get("extra", {})
    # ``extra`` itself is decoded to a dict (single json.loads), but the
    # inner schemas_allowed_for_csv_upload stays a JSON-encoded string —
    # the dataset-export path performs NO parse_extra() double-decode.
    assert isinstance(extra, dict)
    assert extra.get("schemas_allowed_for_csv_upload") == _json.dumps(
        ["schema1", "schema2"]
    )


async def test_export_chart_db_extra_keeps_schemas_allowed_for_file_upload(mock_dao):
    """``schemas_allowed_for_file_upload`` must stay unrenamed in chart export.

    The ``schemas_allowed_for_file_upload`` →
    ``schemas_allowed_for_csv_upload`` rename
    (superset_old/commands/database/export.py:86-89) is performed ONLY by
    ``ExportDatabasesCommand``. Chart export delegates the database YAML to
    ``ExportDatasetsCommand`` (superset_old/commands/chart/export.py:104),
    which emits the extra dict as-is after a single ``json.loads``."""
    import json as _json
    import zipfile

    import yaml

    chart = MagicMock()
    chart.id = 2
    chart.slice_name = "Chart2"
    chart.tags = []
    chart.export_to_dict.return_value = {"slice_name": "Chart2", "viz_type": "table"}

    db = MagicMock()
    db.id = 20
    db.database_name = "OtherDB"
    db.uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    db.export_to_dict.return_value = {
        "database_name": "OtherDB",
        "sqlalchemy_uri": "postgresql://y",
        "extra": _json.dumps({"schemas_allowed_for_file_upload": ["s1", "s2"]}),
    }

    dataset = MagicMock()
    dataset.id = 6
    dataset.table_name = "tbl"
    dataset.uuid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    dataset.database = db
    dataset.export_to_dict.return_value = {"table_name": "tbl", "sql": None}

    chart.table = dataset

    _exec_returns(mock_dao, one=chart)
    mock_dao.count = AsyncMock(return_value=1)

    with (
        patch("superset.db.daos.database.AsyncSSHTunnelDAO", side_effect=ImportError),
        patch(
            "superset.db.filters.chart_access_filters",
            AsyncMock(return_value=[]),
        ),
        patch("superset.utils.feature_flags.feature_flag_manager") as ff,
    ):
        ff.is_feature_enabled.return_value = False
        cmd = ExportChartsCommand(model_ids=[2], dao=mock_dao)
        buf = await cmd.execute()

    with zipfile.ZipFile(buf) as zf:
        db_files = [n for n in zf.namelist() if n.startswith("databases/")]
        assert db_files, "no databases/*.yaml emitted"
        db_yaml = yaml.safe_load(zf.read(db_files[0]))

    extra = db_yaml.get("extra", {})
    # The key keeps its modern name — no V1 back-rename in the dataset path.
    assert extra.get("schemas_allowed_for_file_upload") == ["s1", "s2"]
    assert "schemas_allowed_for_csv_upload" not in extra, (
        "the database-export rename must NOT leak into chart export"
    )


async def test_import_charts_validate_skips_non_dict_config(mock_dao):
    """Regression: a bundled YAML file parsing to a list/scalar must be skipped
    in _validate (was ``config.get`` on a list → AttributeError → HTTP 500)."""
    from superset.commands.chart.importers.v1 import ImportChartsCommand

    cmd = ImportChartsCommand(contents=io.BytesIO(b""), dao=mock_dao)
    # Non-dict ``charts/`` configs must not raise.
    await cmd._validate({"charts/x.yaml": [1, 2, 3], "metadata.yaml": {}})
    await cmd._validate({"charts/x.yaml": "a string"})
    # A real dict still validated: missing slice_name → CommandInvalidError.
    with pytest.raises(CommandInvalidError, match="Missing slice_name"):
        await cmd._validate({"charts/y.yaml": {"viz_type": "table"}})
