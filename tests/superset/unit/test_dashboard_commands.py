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

from superset.commands.dashboard import (
    BulkDeleteDashboardsCommand,
    CopyDashboardCommand,
    CreateDashboardCommand,
    DeleteDashboardCommand,
    DeleteEmbeddedDashboardCommand,
    ExportDashboardsCommand,
    ImportDashboardsCommand,
    UpdateDashboardColorsCommand,
    UpdateDashboardCommand,
    UpdateDashboardFiltersCommand,
    UpsertEmbeddedDashboardCommand,
)
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetSecurityException,
)


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
    return dao


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
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateDashboardCommand(
        dao=mock_dao, dashboard_id=999, data={"dashboard_title": "X"}
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_dashboard_slug_conflict(mock_dao, mock_dashboard):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    mock_dao.validate_update_slug_uniqueness = AsyncMock(return_value=False)
    cmd = UpdateDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={"slug": "taken"},
    )
    with pytest.raises(CommandInvalidError, match="slug.*already exists"):
        await cmd.validate()


async def test_update_dashboard_success(mock_dao, mock_dashboard):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    mock_dao.validate_update_slug_uniqueness = AsyncMock(return_value=True)
    cmd = UpdateDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={"dashboard_title": "Updated", "slug": "updated-slug"},
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.dashboard_title == "Updated"
    mock_dao.session.flush.assert_awaited_once()


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
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=None)
    cmd = CopyDashboardCommand(
        dao=mock_dao,
        dashboard_id=999,
        data={"dashboard_title": "Copy"},
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_copy_dashboard_missing_title(mock_dao, mock_dashboard):
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=mock_dashboard)
    cmd = CopyDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={},
    )
    with pytest.raises(CommandInvalidError, match="dashboard_title.*required"):
        await cmd.validate()


async def test_copy_dashboard_success(mock_dao, mock_dashboard):
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=mock_dashboard)
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

    metadata = json.loads(result.json_metadata)
    assert metadata["native_filter_configuration"] == []


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
        mock_dashboard, {"color_scheme": "supersetColors"}
    )


# ---------------------------------------------------------------------------
# ExportDashboardsCommand
# ---------------------------------------------------------------------------


async def test_export_dashboards_produces_zip(mock_dao, mock_dashboard):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
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
    mock_dao.find_by_id = AsyncMock(return_value=None)
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "metadata.yaml",
            yaml.safe_dump({"version": "1.0.0", "type": "Dashboard"}),
        )
        zf.writestr(
            "dashboards/test.yaml",
            yaml.safe_dump({"dashboard_title": dashboard_title}),
        )
    buf.seek(0)
    return buf


async def test_import_dashboards_success(mock_dao):
    buf = _make_import_zip("Imported Dashboard")
    cmd = ImportDashboardsCommand(contents=buf, dao=mock_dao)
    with patch(
        "superset.commands.dashboard.ImportDashboardsCommand._import_single",
        new_callable=AsyncMock,
    ) as mock_import:
        await cmd.execute()
        mock_import.assert_awaited_once()


async def test_import_dashboards_missing_title(mock_dao):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("metadata.yaml", yaml.safe_dump({"version": "1.0.0"}))
        zf.writestr("dashboards/bad.yaml", yaml.safe_dump({"slug": "no-title"}))
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

    metadata = json.loads(result.json_metadata)
    nfc = metadata["native_filter_configuration"]
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
    """Role IDs are resolved via security_manager on update."""
    mock_sm = AsyncMock()
    role_obj = MagicMock()
    role_obj.id = 5
    mock_sm.find_role_by_id = AsyncMock(return_value=role_obj)
    mock_sm.find_user_by_id = AsyncMock(return_value=None)

    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    mock_dao.validate_update_slug_uniqueness = AsyncMock(return_value=True)

    cmd = UpdateDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={"roles": [5]},
        user_id=1,
        security_manager=mock_sm,
    )
    await cmd.validate()
    result = await cmd.run()

    mock_sm.find_role_by_id.assert_awaited_with(5)
    assert result.roles == [role_obj]


# ---------------------------------------------------------------------------
# Ownership checks
# ---------------------------------------------------------------------------


async def test_delete_non_owner_raises_forbidden(mock_dao, mock_dashboard):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dashboard)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=SupersetSecurityException(message="You don't have permission")
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
        side_effect=SupersetSecurityException(message="You don't have permission")
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
