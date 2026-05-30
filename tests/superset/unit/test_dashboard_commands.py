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

from superset.commands.dashboard.copy import CopyDashboardCommand
from superset.commands.dashboard.create import CreateDashboardCommand
from superset.commands.dashboard.delete import (
    BulkDeleteDashboardsCommand,
    DeleteDashboardCommand,
    DeleteEmbeddedDashboardCommand,
)
from superset.commands.dashboard.embedded.upsert import UpsertEmbeddedDashboardCommand
from superset.commands.dashboard.export import ExportDashboardsCommand
from superset.commands.dashboard.importers.v1 import ImportDashboardsCommand
from superset.commands.dashboard.update import (
    UpdateDashboardColorsCommand,
    UpdateDashboardCommand,
    UpdateDashboardFiltersCommand,
)
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
    dao.session.refresh = AsyncMock()
    # Tag sync / role resolution / report-schedule lookups all go through
    # ``(await session.execute()).scalars().{unique().one_or_none(),all()}``
    # — SYNC chains on the awaited result. A bare AsyncMock makes
    # ``.scalars()`` a coroutine; configure concrete (empty) results so the
    # implicit side-effect queries in run()/validate() don't crash.
    _res = MagicMock()
    _res.scalars.return_value.unique.return_value.one_or_none.return_value = None
    _res.scalars.return_value.unique.return_value.all.return_value = []
    _res.scalars.return_value.one_or_none.return_value = None
    _res.scalars.return_value.all.return_value = []
    _res.fetchall.return_value = []
    dao.session.execute = AsyncMock(return_value=_res)
    dao.session.begin_nested = MagicMock(return_value=AsyncMock())
    return dao


def _exec_returns(mock_dao, *, unique_one=None, one=None, all_=None):
    """Make ``session.execute`` resolve to a concrete result. Commands load via
    ``(await session.execute(stmt)).scalars().one_or_none()`` (export),
    ``.scalars().all()`` (roles / report schedules) — not the ``find_by_id``
    the older tests mocked."""
    res = MagicMock()
    res.scalars.return_value.unique.return_value.one_or_none.return_value = unique_one
    res.scalars.return_value.unique.return_value.all.return_value = all_ or []
    res.scalars.return_value.one_or_none.return_value = (
        one if one is not None else unique_one
    )
    res.scalars.return_value.all.return_value = all_ or []
    res.fetchall.return_value = []
    mock_dao.session.execute = AsyncMock(return_value=res)


@pytest.fixture
def mock_embedded_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
    return dao


@pytest.fixture
def mock_dashboard():
    dashboard = MagicMock()
    dashboard.id = 1
    dashboard.dashboard_title = "Test Dashboard"
    dashboard.slug = "test-dashboard"
    dashboard.position_json = "{}"
    dashboard.css = None
    dashboard.json_metadata = "{}"
    dashboard.published = False
    dashboard.uuid = None
    dashboard.description = "A test dashboard"
    # M2M collections must be real lists — run() iterates owners/roles/tags
    # for tag-sync and export bundles slices; a bare MagicMock isn't iterable.
    dashboard.owners = []
    dashboard.roles = []
    dashboard.tags = []
    dashboard.slices = []
    dashboard.theme = None
    return dashboard


@pytest.fixture
def mock_embedded():
    embedded = MagicMock()
    embedded.uuid = "abc-123"
    embedded.allowed_domains = ["example.com"]
    embedded.dashboard_id = 1
    embedded.changed_on = None
    return embedded


# ---------------------------------------------------------------------------
# CreateDashboardCommand
# ---------------------------------------------------------------------------


async def test_create_dashboard_allows_missing_title(mock_dao):
    """Superset allows creating dashboards without a title; superset should too."""
    mock_dao.validate_slug_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDashboardCommand(dao=mock_dao, data={"slug": "test"})
    # Should not raise — dashboard_title is optional
    await cmd.validate()


async def test_create_dashboard_validates_slug_uniqueness(mock_dao):
    mock_dao.validate_slug_uniqueness = AsyncMock(return_value=False)
    cmd = CreateDashboardCommand(
        dao=mock_dao,
        data={"dashboard_title": "Test", "slug": "taken"},
    )
    with pytest.raises(CommandInvalidError, match="slug.*already exists"):
        await cmd.validate()


async def test_create_dashboard_validates_success(mock_dao):
    mock_dao.validate_slug_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDashboardCommand(
        dao=mock_dao,
        data={"dashboard_title": "Test", "slug": "unique"},
        user_id=1,
    )
    await cmd.validate()  # Should not raise


# ---------------------------------------------------------------------------
# UpdateDashboardCommand
# ---------------------------------------------------------------------------


async def test_update_dashboard_not_found(mock_dao):
    # UpdateDashboardCommand loads via ``find_by_id_with_options`` (eager M2M).
    mock_dao.find_by_id_with_options = AsyncMock(return_value=None)
    cmd = UpdateDashboardCommand(
        dao=mock_dao, dashboard_id=999, data={"dashboard_title": "X"}
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_dashboard_slug_conflict(mock_dao, mock_dashboard):
    mock_dao.find_by_id_with_options = AsyncMock(return_value=mock_dashboard)
    mock_dao.validate_update_slug_uniqueness = AsyncMock(return_value=False)
    cmd = UpdateDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={"slug": "taken"},
    )
    with pytest.raises(CommandInvalidError, match="slug.*already exists"):
        await cmd.validate()


async def test_update_dashboard_success(mock_dao, mock_dashboard):
    mock_dao.find_by_id_with_options = AsyncMock(return_value=mock_dashboard)
    mock_dao.validate_update_slug_uniqueness = AsyncMock(return_value=True)
    cmd = UpdateDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={"dashboard_title": "Updated", "slug": "updated-slug"},
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.dashboard_title == "Updated"
    mock_dao.session.flush.assert_awaited()


# ---------------------------------------------------------------------------
# DeleteDashboardCommand
# ---------------------------------------------------------------------------


async def test_delete_dashboard_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteDashboardCommand(dao=mock_dao, dashboard_id=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_dashboard_success(mock_dao, mock_dashboard):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    mock_dao.find_report_schedules_by_dashboard_id = AsyncMock(return_value=[])
    cmd = DeleteDashboardCommand(dao=mock_dao, dashboard_id=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_dashboard])


# ---------------------------------------------------------------------------
# BulkDeleteDashboardsCommand
# ---------------------------------------------------------------------------


async def test_bulk_delete_empty_ids(mock_dao):
    cmd = BulkDeleteDashboardsCommand(dao=mock_dao, dashboard_ids=[])
    with pytest.raises(CommandInvalidError, match="No dashboard IDs"):
        await cmd.validate()


async def test_bulk_delete_success(mock_dao, mock_dashboard):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_dashboard])
    mock_dao.find_report_schedules_by_dashboard_id = AsyncMock(return_value=[])
    cmd = BulkDeleteDashboardsCommand(dao=mock_dao, dashboard_ids=[1])
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_dashboard])


# ---------------------------------------------------------------------------
# CopyDashboardCommand
# ---------------------------------------------------------------------------


async def test_copy_dashboard_not_found(mock_dao):
    # CopyDashboardCommand loads via ``get_full_by_id_or_slug`` (eager owners).
    mock_dao.get_full_by_id_or_slug = AsyncMock(return_value=None)
    cmd = CopyDashboardCommand(
        dao=mock_dao,
        dashboard_id=999,
        data={"dashboard_title": "Copy"},
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_copy_dashboard_missing_title(mock_dao, mock_dashboard):
    mock_dao.get_full_by_id_or_slug = AsyncMock(return_value=mock_dashboard)
    cmd = CopyDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={},
    )
    with pytest.raises(CommandInvalidError, match="dashboard_title.*required"):
        await cmd.validate()


async def test_copy_dashboard_success(mock_dao, mock_dashboard):
    mock_dao.get_full_by_id_or_slug = AsyncMock(return_value=mock_dashboard)
    new_dash = MagicMock()
    new_dash.id = 2
    new_dash.dashboard_title = "Copy of Test"
    mock_dao.copy_dashboard = AsyncMock(return_value=new_dash)
    cmd = CopyDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={"dashboard_title": "Copy of Test", "json_metadata": "{}"},
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.id == 2
    assert result.dashboard_title == "Copy of Test"
    mock_dao.copy_dashboard.assert_awaited_once()


# ---------------------------------------------------------------------------
# UpdateDashboardFiltersCommand
# ---------------------------------------------------------------------------


async def test_update_filters_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateDashboardFiltersCommand(
        dao=mock_dao,
        dashboard_id=999,
        data={"deleted": [], "modified": [], "reordered": []},
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_filters_empty_data(mock_dao, mock_dashboard):
    """Validate accepts empty filter data (no changes)."""
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    cmd = UpdateDashboardFiltersCommand(dao=mock_dao, dashboard_id=1, data={})
    await cmd.validate()


async def test_update_filters_success(mock_dao, mock_dashboard):
    import json  # noqa: TID251

    # Pre-populate native_filter_configuration with one filter
    mock_dashboard.json_metadata = json.dumps(
        {"native_filter_configuration": [{"id": "f1", "name": "Region"}]}
    )
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    filter_data = {"deleted": ["f1"], "modified": [], "reordered": []}
    cmd = UpdateDashboardFiltersCommand(dao=mock_dao, dashboard_id=1, data=filter_data)
    await cmd.validate()
    result = await cmd.run()

    # run() returns the updated native_filter_configuration list directly.
    assert result == []


# ---------------------------------------------------------------------------
# UpdateDashboardColorsCommand
# ---------------------------------------------------------------------------


async def test_update_colors_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateDashboardColorsCommand(
        dao=mock_dao, dashboard_id=999, data={"color_scheme": "blue"}
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_colors_success(mock_dao, mock_dashboard):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    mock_dao.update_colors_config = AsyncMock()
    cmd = UpdateDashboardColorsCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={"color_scheme": "supersetColors"},
    )
    await cmd.validate()
    await cmd.run()
    mock_dao.update_colors_config.assert_awaited_once_with(
        mock_dashboard, {"color_scheme": "supersetColors"}, mark_updated=True
    )


# ---------------------------------------------------------------------------
# ExportDashboardsCommand
# ---------------------------------------------------------------------------


async def test_export_dashboards_produces_zip(mock_dao, mock_dashboard):
    # Export loads via session.execute().scalars().one_or_none() and builds the
    # YAML from export_to_dict (not field reads); no slices/theme to bundle.
    mock_dashboard.export_to_dict.return_value = {
        "dashboard_title": "Test Dashboard",
        "slug": "test-dashboard",
    }
    _exec_returns(mock_dao, one=mock_dashboard)
    cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao)
    buf = await cmd.execute()
    assert isinstance(buf, io.BytesIO)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        assert any("dashboards/" in n for n in names)
        assert "metadata.yaml" in names
        # Verify YAML content contains known fields
        dash_files = [n for n in names if n.startswith("dashboards/")]
        content = yaml.safe_load(zf.read(dash_files[0]))
        assert content["dashboard_title"] == "Test Dashboard"
        assert content["slug"] == "test-dashboard"


async def test_export_dashboards_not_found(mock_dao):
    _exec_returns(mock_dao, one=None)
    cmd = ExportDashboardsCommand(model_ids=[999], dao=mock_dao)
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


async def test_export_dashboards_no_dao():
    cmd = ExportDashboardsCommand(model_ids=[1], dao=None)
    with pytest.raises(CommandInvalidError, match="DAO not provided"):
        await cmd.execute()


# ---------------------------------------------------------------------------
# ImportDashboardsCommand
# ---------------------------------------------------------------------------


def _make_import_zip(dashboard_title: str = "Imported") -> io.BytesIO:
    # Real export bundles are wrapped in a top-level export directory which
    # ``_parse_zip`` strips (``remove_root``). Without it, ``dashboards/x.yaml``
    # collapses to ``x.yaml`` and the importer's ``dashboards/`` prefix checks
    # never match.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "bundle/metadata.yaml",
            yaml.safe_dump({"version": "1.0.0", "type": "Dashboard"}),
        )
        zf.writestr(
            "bundle/dashboards/test.yaml",
            yaml.safe_dump({"dashboard_title": dashboard_title}),
        )
    buf.seek(0)
    return buf


async def test_import_dashboards_success(mock_dao):
    # ``ImportDashboardsCommand`` overrides ``run()`` with a bespoke
    # databases->datasets->charts->dashboards orchestration (it does NOT use
    # the base ``_import_single`` loop). The stable unit-level contract is that
    # ``validate()`` accepts a well-formed bundle: the ZIP parses, metadata.yaml
    # version/type check passes, and ``_validate`` finds the dashboard_title.
    # End-to-end ``run()`` needs a realistic multi-file bundle (uuids, position,
    # datasets) and is covered by the integration suite.
    buf = _make_import_zip("Imported Dashboard")
    cmd = ImportDashboardsCommand(contents=buf, dao=mock_dao)
    await cmd.validate()
    assert "dashboards/test.yaml" in cmd._configs
    assert cmd._configs["dashboards/test.yaml"]["dashboard_title"] == (
        "Imported Dashboard"
    )


async def test_import_dashboards_missing_title(mock_dao):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bundle/metadata.yaml", yaml.safe_dump({"version": "1.0.0"}))
        zf.writestr(
            "bundle/dashboards/bad.yaml", yaml.safe_dump({"slug": "no-title"})
        )
    buf.seek(0)
    cmd = ImportDashboardsCommand(contents=buf, dao=mock_dao)
    with pytest.raises(CommandInvalidError, match="Missing dashboard_title"):
        await cmd.execute()


async def test_import_dashboards_no_dao():
    buf = _make_import_zip()
    cmd = ImportDashboardsCommand(contents=buf, dao=None)
    with pytest.raises(CommandInvalidError, match="DAO not provided"):
        await cmd.execute()


# ---------------------------------------------------------------------------
# UpsertEmbeddedDashboardCommand
# ---------------------------------------------------------------------------


async def test_create_embedded_not_found(mock_dao, mock_embedded_dao):
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=None)
    cmd = UpsertEmbeddedDashboardCommand(
        dao=mock_dao,
        embedded_dao=mock_embedded_dao,
        dashboard_id=999,
        allowed_domains=["example.com"],
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_create_embedded_success(
    mock_dao, mock_embedded_dao, mock_dashboard, mock_embedded
):
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=mock_dashboard)
    mock_embedded_dao.upsert = AsyncMock(return_value=mock_embedded)
    cmd = UpsertEmbeddedDashboardCommand(
        dao=mock_dao,
        embedded_dao=mock_embedded_dao,
        dashboard_id=1,
        allowed_domains=["example.com"],
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.uuid == "abc-123"
    mock_embedded_dao.upsert.assert_awaited_once_with(1, ["example.com"])


# ---------------------------------------------------------------------------
# DeleteEmbeddedDashboardCommand
# ---------------------------------------------------------------------------


async def test_delete_embedded_not_found(mock_dao, mock_embedded_dao):
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=None)
    cmd = DeleteEmbeddedDashboardCommand(
        dao=mock_dao,
        embedded_dao=mock_embedded_dao,
        dashboard_id=999,
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_embedded_success(
    mock_dao, mock_embedded_dao, mock_dashboard, mock_embedded
):
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=mock_dashboard)
    mock_embedded_dao.find_by_dashboard_id = AsyncMock(return_value=mock_embedded)
    cmd = DeleteEmbeddedDashboardCommand(
        dao=mock_dao,
        embedded_dao=mock_embedded_dao,
        dashboard_id=1,
    )
    await cmd.validate()
    await cmd.run()
    mock_embedded_dao.session.delete.assert_awaited_once_with(mock_embedded)


async def test_delete_embedded_no_existing(mock_dao, mock_embedded_dao, mock_dashboard):
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=mock_dashboard)
    mock_embedded_dao.find_by_dashboard_id = AsyncMock(return_value=None)
    cmd = DeleteEmbeddedDashboardCommand(
        dao=mock_dao,
        embedded_dao=mock_embedded_dao,
        dashboard_id=1,
    )
    await cmd.validate()
    await cmd.run()  # Should not raise
    mock_embedded_dao.session.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# T1-6: UpdateDashboardFiltersCommand — native_filter_configuration
# ---------------------------------------------------------------------------


async def test_update_filters_modifies_nfc(mock_dao, mock_dashboard):
    """Filters are properly processed in native_filter_configuration."""
    import json  # noqa: TID251

    mock_dashboard.json_metadata = json.dumps(
        {
            "native_filter_configuration": [
                {"id": "f1", "name": "Region"},
                {"id": "f2", "name": "Country"},
                {"id": "f3", "name": "City"},
            ]
        }
    )
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)

    filter_data = {
        "deleted": ["f1"],
        "modified": [{"id": "f2", "name": "Country (updated)"}],
        "reordered": ["f3", "f2"],
    }
    cmd = UpdateDashboardFiltersCommand(dao=mock_dao, dashboard_id=1, data=filter_data)
    await cmd.validate()
    result = await cmd.run()

    # run() returns the updated native_filter_configuration list directly.
    nfc = result
    # f1 deleted, f2 modified, reordered: f3 first then f2
    assert len(nfc) == 2
    assert nfc[0]["id"] == "f3"
    assert nfc[1]["id"] == "f2"
    assert nfc[1]["name"] == "Country (updated)"


# ---------------------------------------------------------------------------
# T1-7: owners / roles resolution
# ---------------------------------------------------------------------------


async def test_create_resolves_owners(mock_dao):
    """Owner IDs are resolved via security_manager on create."""
    mock_sm = AsyncMock()
    user_obj = MagicMock()
    user_obj.id = 10
    mock_sm.find_user_by_id = AsyncMock(return_value=user_obj)

    cmd = CreateDashboardCommand(
        dao=mock_dao,
        data={"dashboard_title": "Owned", "owners": [10]},
        user_id=1,
        security_manager=mock_sm,
    )
    mock_dao.validate_slug_uniqueness = AsyncMock(return_value=True)
    await cmd.validate()

    mock_dashboard_cls = MagicMock()
    instance = MagicMock()
    instance.owners = []
    instance.roles = []
    mock_dashboard_cls.return_value = instance
    with patch.dict(
        "sys.modules",
        {"superset.models.dashboard": MagicMock(Dashboard=mock_dashboard_cls)},
    ):
        result = await cmd.run()

    mock_sm.find_user_by_id.assert_awaited_with(10)
    assert result.owners == [user_obj]


async def test_update_applies_roles(mock_dao, mock_dashboard):
    """Role IDs are resolved via ``populate_roles`` (session query) on update."""
    mock_sm = AsyncMock()
    role_obj = MagicMock()
    role_obj.id = 5
    mock_sm.find_user_by_id = AsyncMock(return_value=None)

    mock_dao.find_by_id_with_options = AsyncMock(return_value=mock_dashboard)
    mock_dao.validate_update_slug_uniqueness = AsyncMock(return_value=True)
    # ``populate_roles`` resolves ids via ``select(Role).where(id.in_(...))``
    # -> session.execute().scalars().all(); return exactly the requested role.
    _exec_returns(mock_dao, all_=[role_obj])

    cmd = UpdateDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={"roles": [5]},
        user_id=1,
        security_manager=mock_sm,
    )
    await cmd.validate()
    result = await cmd.run()

    assert result.roles == [role_obj]


# ---------------------------------------------------------------------------
# Ownership checks
# ---------------------------------------------------------------------------


async def test_delete_non_owner_raises_forbidden(mock_dao, mock_dashboard):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=_security_exception("You don't have permission")
    )
    cmd = DeleteDashboardCommand(
        dao=mock_dao, dashboard_id=1, security_manager=sm, user_id=42
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


async def test_update_non_owner_raises_forbidden(mock_dao, mock_dashboard):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=_security_exception("You don't have permission")
    )
    cmd = UpdateDashboardCommand(
        dao=mock_dao, dashboard_id=1, data={}, user_id=42, security_manager=sm
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T6: BulkDelete — "some IDs not found" branch
# ---------------------------------------------------------------------------


async def test_bulk_delete_some_ids_not_found(mock_dao, mock_dashboard):
    """BulkDeleteDashboardsCommand raises when some IDs are missing."""
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_dashboard])  # only id=1 found
    mock_dao.find_report_schedules_by_dashboard_id = AsyncMock(return_value=[])
    cmd = BulkDeleteDashboardsCommand(dao=mock_dao, dashboard_ids=[1, 2, 3])
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()
