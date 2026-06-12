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
    """Slug conflict is the field-keyed 422 upstream emits:
    ``DashboardInvalidError(exceptions=[DashboardSlugExistsValidationError()])``
    → ``{"slug": ["Must be unique"]}`` (superset_old/commands/dashboard/
    exceptions.py:33-39), not a flat string."""
    from superset.commands.dashboard.exceptions import DashboardInvalidError

    mock_dao.validate_slug_uniqueness = AsyncMock(return_value=False)
    cmd = CreateDashboardCommand(
        dao=mock_dao,
        data={"dashboard_title": "Test", "slug": "taken"},
    )
    with pytest.raises(DashboardInvalidError) as exc_info:
        await cmd.validate()
    assert exc_info.value.normalized_messages() == {"slug": ["Must be unique"]}


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
    """PUT slug conflict — same field-keyed shape as create (upstream
    UpdateDashboardCommand.validate collects DashboardSlugExistsValidationError
    into DashboardInvalidError)."""
    from superset.commands.dashboard.exceptions import DashboardInvalidError

    mock_dao.find_by_id_with_options = AsyncMock(return_value=mock_dashboard)
    mock_dao.validate_update_slug_uniqueness = AsyncMock(return_value=False)
    cmd = UpdateDashboardCommand(
        dao=mock_dao,
        dashboard_id=1,
        data={"slug": "taken"},
    )
    with pytest.raises(DashboardInvalidError) as exc_info:
        await cmd.validate()
    assert exc_info.value.normalized_messages() == {"slug": ["Must be unique"]}


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


async def test_update_dashboard_tags_applied_without_tagging_system_flag(
    mock_dao, mock_dashboard
):
    """Explicit tags in a PUT payload are validated and applied regardless of
    TAGGING_SYSTEM — upstream calls ``validate_tags``/``update_tags``
    unconditionally (superset_old/commands/dashboard/update.py:64-65,106-110);
    the flag only gates the implicit owner/type tag event-listeners."""
    mock_dao.find_by_id_with_options = AsyncMock(return_value=mock_dashboard)
    actor = MagicMock()
    actor.id = 1
    sm = AsyncMock()
    sm.is_admin = MagicMock(return_value=True)
    sm.find_user_by_id = AsyncMock(return_value=actor)
    with (
        patch(
            "superset.commands.dashboard.update.feature_flag_manager.is_feature_enabled",
            return_value=False,
        ),
        patch(
            "superset.commands.dashboard.update.validate_tags", new=AsyncMock()
        ) as vt,
        patch("superset.commands.dashboard.update.update_tags", new=AsyncMock()) as ut,
    ):
        cmd = UpdateDashboardCommand(
            dao=mock_dao,
            dashboard_id=1,
            data={"tags": [5]},
            user_id=1,
            security_manager=sm,
        )
        await cmd.validate()
        await cmd.run()
    vt.assert_awaited_once()
    ut.assert_awaited_once()


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
    # validate() calls dao.count() after the access-filter check; return 1 for
    # the single requested ID so the access gate passes.
    mock_dao.count = AsyncMock(return_value=1)
    cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao)
    with patch(
        "superset.db.filters.dashboard_access_filters",
        AsyncMock(return_value=[]),
    ):
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
    # count=0 → validate() sees the ID as inaccessible and raises before
    # _export_single is ever called (same observable result: ObjectNotFoundError).
    mock_dao.count = AsyncMock(return_value=0)
    cmd = ExportDashboardsCommand(model_ids=[999], dao=mock_dao)
    with patch(
        "superset.db.filters.dashboard_access_filters",
        AsyncMock(return_value=[]),
    ):
        with pytest.raises(ObjectNotFoundError):
            await cmd.execute()


async def test_export_dashboards_non_object_position_json(mock_dao, mock_dashboard):
    """Regression: a non-object position_json / json_metadata must not 500.

    The exporter decodes these JSON-string fields then does dict ops
    (``metadata.get(...)`` / ``find_chart_uuids(position)``). A valid-but-
    non-object value (``"[1,2,3]"``) previously raised → HTTP 500 on export.
    """
    mock_dashboard.export_to_dict.return_value = {
        "dashboard_title": "Test Dashboard",
        "slug": "test-dashboard",
        "position_json": "[1, 2, 3]",
        "json_metadata": '"a string"',
    }
    _exec_returns(mock_dao, one=mock_dashboard)
    # Access gate: 1 accessible ID out of 1 requested → passes.
    mock_dao.count = AsyncMock(return_value=1)
    cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao)
    with patch(
        "superset.db.filters.dashboard_access_filters",
        AsyncMock(return_value=[]),
    ):
        buf = await cmd.execute()
    assert isinstance(buf, io.BytesIO)
    with zipfile.ZipFile(buf) as zf:
        assert any(n.startswith("dashboards/") for n in zf.namelist())


async def test_export_dashboards_no_dao():
    cmd = ExportDashboardsCommand(model_ids=[1], dao=None)
    with pytest.raises(CommandInvalidError, match="DAO not provided"):
        await cmd.execute()


async def test_export_dashboards_includes_ssh_tunnel_in_database_yaml(
    mock_dao, mock_dashboard
):
    """databases/*.yaml in a dashboard bundle must carry the masked SSH tunnel.

    1:1 with the original: dashboard export delegates database YAMLs to
    ``ExportDatasetsCommand._export`` (superset_old/commands/dashboard/
    export.py → chart/export.py:104 → dataset/export.py:114-121), which adds
    ``payload["ssh_tunnel"] = mask_password_info(...)`` whenever
    ``DatabaseDAO.get_ssh_tunnel(database_id)`` returns a tunnel.
    """
    from superset.constants import PASSWORD_MASK

    db = MagicMock()
    db.id = 10
    db.database_name = "TunnelDB"
    db.uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    db.export_to_dict.return_value = {
        "database_name": "TunnelDB",
        "sqlalchemy_uri": "postgresql://x",
    }

    dataset = MagicMock()
    dataset.id = 5
    dataset.table_name = "tbl"
    dataset.uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    dataset.database = db
    dataset.export_to_dict.return_value = {"table_name": "tbl", "sql": None}

    chart = MagicMock()
    chart.id = 7
    chart.slice_name = "Chart"
    chart.uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    chart.tags = []
    chart.table = dataset
    chart.export_to_dict.return_value = {"slice_name": "Chart", "viz_type": "table"}

    mock_dashboard.slices = [chart]
    mock_dashboard.export_to_dict.return_value = {
        "dashboard_title": "Test Dashboard",
        "slug": "test-dashboard",
    }
    _exec_returns(mock_dao, one=mock_dashboard)
    mock_dao.count = AsyncMock(return_value=1)

    tunnel = MagicMock()
    tunnel.export_to_dict.return_value = {
        "server_address": "ssh.example.com",
        "server_port": 22,
        "username": "tunnel_user",
        "password": "super-secret",
    }
    ssh_dao_cls = MagicMock()
    ssh_dao_cls.return_value.get_by_database_id = AsyncMock(return_value=tunnel)

    with (
        patch(
            "superset.db.filters.dashboard_access_filters",
            AsyncMock(return_value=[]),
        ),
        patch("superset.db.daos.database.AsyncSSHTunnelDAO", ssh_dao_cls),
    ):
        cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao)
        buf = await cmd.execute()

    with zipfile.ZipFile(buf) as zf:
        db_files = [n for n in zf.namelist() if n.startswith("databases/")]
        assert db_files, "no databases/*.yaml emitted"
        db_yaml = yaml.safe_load(zf.read(db_files[0]))

    assert "ssh_tunnel" in db_yaml, "ssh_tunnel must be embedded in database YAML"
    assert db_yaml["ssh_tunnel"]["server_address"] == "ssh.example.com"
    assert db_yaml["ssh_tunnel"]["username"] == "tunnel_user"
    # Secrets must be masked, never exported in clear text.
    assert db_yaml["ssh_tunnel"]["password"] == PASSWORD_MASK


# ---------------------------------------------------------------------------
# MEDIUM fix: export_related=False suppresses tags.yaml and theme files
# Original: superset_old/commands/dashboard/export.py:187 gates ALL related
# emissions (charts, tags, themes) inside ``if export_related:``.
# superset_old/commands/export/assets.py:59 always calls command with
# export_related=False so the full-assets bundle never contains tags.yaml
# or theme YAML files from the dashboard exporter.
# ---------------------------------------------------------------------------


async def test_export_dashboards_export_related_false_suppresses_tags_yaml(
    mock_dao, mock_dashboard
):
    """When export_related=False, _export_single must NOT emit tags.yaml.

    The original ExportAssetsCommand (superset_old/commands/export/assets.py:59)
    passes export_related=False to every per-resource command.  The original
    ExportDashboardsCommand._export gates the tags.yaml emission inside
    ``if export_related:`` (superset_old/commands/dashboard/export.py:187-197).
    A full-assets bundle therefore never contains tags.yaml; liteset must
    reproduce the same absence.
    """
    mock_dashboard.export_to_dict.return_value = {
        "dashboard_title": "Dash With Tags",
    }
    _exec_returns(mock_dao, one=mock_dashboard)

    cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao, export_related=False)

    with patch("superset.commands.dashboard.export.feature_flag_manager") as mock_ffm:
        mock_ffm.is_feature_enabled.return_value = True
        files = await cmd._export_single(1)  # noqa: SLF001

    file_names = [f for f, _ in files]
    assert "tags.yaml" not in file_names, (
        "export_related=False must suppress tags.yaml (1:1 with original "
        "superset_old/commands/dashboard/export.py:187 if export_related: guard)"
    )


async def test_export_dashboards_export_related_true_emits_tags_yaml(
    mock_dao, mock_dashboard
):
    """When export_related=True (default), _export_single MUST emit tags.yaml
    when TAGGING_SYSTEM is enabled.  This confirms the flag correctly gates
    rather than unconditionally blocking.
    """
    tag = MagicMock()
    tag.name = "my-tag"
    tag.description = "a tag"
    from superset.models.tags import TagType

    tag.type = TagType.custom

    mock_dashboard.export_to_dict.return_value = {
        "dashboard_title": "Dash With Tags",
    }
    mock_dashboard.tags = [tag]
    _exec_returns(mock_dao, one=mock_dashboard)

    cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao, export_related=True)

    with patch("superset.commands.dashboard.export.feature_flag_manager") as mock_ffm:
        mock_ffm.is_feature_enabled.return_value = True
        files = await cmd._export_single(1)  # noqa: SLF001

    file_names = [f for f, _ in files]
    assert "tags.yaml" in file_names, (
        "export_related=True must emit tags.yaml when TAGGING_SYSTEM is enabled"
    )


async def test_export_dashboards_export_related_false_suppresses_theme_files(
    mock_dao, mock_dashboard
):
    """When export_related=False, _export_single must NOT emit any theme YAML files.

    The original ExportDashboardsCommand._export gates the theme export inside
    ``if export_related:`` (superset_old/commands/dashboard/export.py:199-203).
    The full-assets bundle calls every per-resource command with export_related=False
    (superset_old/commands/export/assets.py:59), so no theme files are injected
    by the dashboard exporter into the bundle.
    """
    theme = MagicMock()
    theme.id = 7
    theme.uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    mock_dashboard.theme = theme

    mock_dashboard.export_to_dict.return_value = {
        "dashboard_title": "Dash With Theme",
    }
    _exec_returns(mock_dao, one=mock_dashboard)

    cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao, export_related=False)

    with patch("superset.commands.dashboard.export.feature_flag_manager") as mock_ffm:
        mock_ffm.is_feature_enabled.return_value = False
        files = await cmd._export_single(1)  # noqa: SLF001

    file_names = [f for f, _ in files]
    theme_files = [f for f in file_names if f.startswith("themes/")]
    assert not theme_files, (
        "export_related=False must suppress theme YAML files (1:1 with original "
        "superset_old/commands/dashboard/export.py:199-203 if export_related: guard)"
    )


async def test_export_dashboards_export_related_true_emits_theme_files(
    mock_dao, mock_dashboard
):
    """When export_related=True (default) and the dashboard has a theme,
    _export_single MUST emit the theme YAML file.
    """
    theme = MagicMock()
    theme.id = 7
    theme.uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    mock_dashboard.theme = theme

    mock_dashboard.export_to_dict.return_value = {
        "dashboard_title": "Dash With Theme",
    }
    _exec_returns(mock_dao, one=mock_dashboard)

    # Stub ExportThemesCommand so we don't need a real DAO/model.
    # ExportThemesCommand is imported lazily inside _export_single via
    # ``from superset.commands.theme import ExportThemesCommand`` — patch
    # it at the source module so the local import picks up the stub.
    fake_theme_files: list[tuple[str, str]] = [("themes/MyTheme.yaml", "version: 1")]

    mock_export_instance = AsyncMock()
    mock_export_instance.run = AsyncMock(return_value=fake_theme_files)

    with (
        patch("superset.commands.dashboard.export.feature_flag_manager") as mock_ffm,
        patch(
            "superset.commands.theme.ExportThemesCommand",
            return_value=mock_export_instance,
        ),
        patch("superset.db.daos.theme.AsyncThemeDAO"),
    ):
        mock_ffm.is_feature_enabled.return_value = False

        cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao, export_related=True)
        files = await cmd._export_single(1)  # noqa: SLF001

    file_names = [f for f, _ in files]
    assert "themes/MyTheme.yaml" in file_names, (
        "export_related=True must emit theme YAML files when dashboard has a theme"
    )


async def test_export_dashboards_denies_inaccessible_id(mock_dao):
    """validate() raises ObjectNotFoundError when the requested dashboard ID is
    not accessible to the current user (count < len(model_ids))."""
    # count=0 simulates an ID the security filter excludes entirely.
    mock_dao.count = AsyncMock(return_value=0)
    cmd = ExportDashboardsCommand(
        model_ids=[42], dao=mock_dao, security_manager=MagicMock()
    )
    with patch(
        "superset.db.filters.dashboard_access_filters",
        AsyncMock(return_value=[]),
    ):
        with pytest.raises(ObjectNotFoundError):
            await cmd.validate()


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
        zf.writestr("bundle/dashboards/bad.yaml", yaml.safe_dump({"slug": "no-title"}))
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


# ---------------------------------------------------------------------------
# MEDIUM fix 1: BulkDelete cleans up TaggedObject rows (TAGGING_SYSTEM gated)
# ---------------------------------------------------------------------------


async def test_bulk_delete_calls_delete_tagged_objects_when_flag_on(
    mock_dao, mock_dashboard
):
    """BulkDeleteDashboardsCommand.run() removes tagged_object rows per dashboard
    when TAGGING_SYSTEM is enabled — 1:1 with DashboardUpdater.after_delete."""
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_dashboard])

    cmd = BulkDeleteDashboardsCommand(dao=mock_dao, dashboard_ids=[1])
    cmd._dashboards = [mock_dashboard]

    with (
        patch("superset.commands.dashboard.delete.feature_flag_manager") as mock_ffm,
        patch(
            "superset.commands.dashboard.delete.delete_tagged_objects",
            new_callable=AsyncMock,
        ) as mock_dtm,
    ):
        mock_ffm.is_feature_enabled.return_value = True
        await cmd.run()

    mock_ffm.is_feature_enabled.assert_called_with("TAGGING_SYSTEM")
    mock_dtm.assert_awaited_once_with(mock_dao.session, "dashboard", mock_dashboard.id)
    mock_dao.delete.assert_awaited_once_with([mock_dashboard])


async def test_bulk_delete_skips_delete_tagged_objects_when_flag_off(
    mock_dao, mock_dashboard
):
    """BulkDeleteDashboardsCommand.run() does NOT touch tagged_object rows when
    TAGGING_SYSTEM is disabled — mirrors the original behavior (SQLA event
    listeners only registered when the flag is on)."""
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_dashboard])

    cmd = BulkDeleteDashboardsCommand(dao=mock_dao, dashboard_ids=[1])
    cmd._dashboards = [mock_dashboard]

    with (
        patch("superset.commands.dashboard.delete.feature_flag_manager") as mock_ffm,
        patch(
            "superset.commands.dashboard.delete.delete_tagged_objects",
            new_callable=AsyncMock,
        ) as mock_dtm,
    ):
        mock_ffm.is_feature_enabled.return_value = False
        await cmd.run()

    mock_dtm.assert_not_awaited()
    mock_dao.delete.assert_awaited_once_with([mock_dashboard])


# ---------------------------------------------------------------------------
# MEDIUM fix 3: TAGGING_SYSTEM gate on delete single dashboard
# ---------------------------------------------------------------------------


async def test_delete_dashboard_calls_delete_tagged_when_flag_on(
    mock_dao, mock_dashboard
):
    """DeleteDashboardCommand.run() removes tagged_object rows when TAGGING_SYSTEM
    is on."""
    cmd = DeleteDashboardCommand(dao=mock_dao, dashboard_id=1)
    cmd._dashboard = mock_dashboard

    with (
        patch("superset.commands.dashboard.delete.feature_flag_manager") as mock_ffm,
        patch(
            "superset.commands.dashboard.delete.delete_tagged_objects",
            new_callable=AsyncMock,
        ) as mock_dtm,
    ):
        mock_ffm.is_feature_enabled.return_value = True
        await cmd.run()

    mock_dtm.assert_awaited_once_with(mock_dao.session, "dashboard", mock_dashboard.id)


async def test_delete_dashboard_skips_delete_tagged_when_flag_off(
    mock_dao, mock_dashboard
):
    """DeleteDashboardCommand.run() does NOT touch tagged_object rows when
    TAGGING_SYSTEM is disabled."""
    cmd = DeleteDashboardCommand(dao=mock_dao, dashboard_id=1)
    cmd._dashboard = mock_dashboard

    with (
        patch("superset.commands.dashboard.delete.feature_flag_manager") as mock_ffm,
        patch(
            "superset.commands.dashboard.delete.delete_tagged_objects",
            new_callable=AsyncMock,
        ) as mock_dtm,
    ):
        mock_ffm.is_feature_enabled.return_value = False
        await cmd.run()

    mock_dtm.assert_not_awaited()


# ---------------------------------------------------------------------------
# MEDIUM fix 3: TAGGING_SYSTEM gate on create
# ---------------------------------------------------------------------------


async def test_create_dashboard_skips_implicit_tags_when_flag_off(mock_dao):
    """CreateDashboardCommand.run() does NOT call add_implicit_tags when
    TAGGING_SYSTEM is disabled."""
    mock_dao.validate_slug_uniqueness = AsyncMock(return_value=True)

    cmd = CreateDashboardCommand(dao=mock_dao, data={"dashboard_title": "Test"})

    mock_dashboard_cls = MagicMock()
    instance = MagicMock()
    instance.id = 1
    instance.owners = []
    instance.roles = []
    mock_dashboard_cls.return_value = instance

    with (
        patch.dict(
            "sys.modules",
            {"superset.models.dashboard": MagicMock(Dashboard=mock_dashboard_cls)},
        ),
        patch("superset.commands.dashboard.create.feature_flag_manager") as mock_ffm,
        patch(
            "superset.commands.dashboard.create.add_implicit_tags_after_insert",
            new_callable=AsyncMock,
        ) as mock_tags,
    ):
        mock_ffm.is_feature_enabled.return_value = False
        await cmd.run()

    mock_tags.assert_not_awaited()


async def test_create_dashboard_calls_implicit_tags_when_flag_on(mock_dao):
    """CreateDashboardCommand.run() calls add_implicit_tags when TAGGING_SYSTEM
    is on."""
    mock_dao.validate_slug_uniqueness = AsyncMock(return_value=True)

    cmd = CreateDashboardCommand(dao=mock_dao, data={"dashboard_title": "Test"})

    mock_dashboard_cls = MagicMock()
    instance = MagicMock()
    instance.id = 1
    instance.owners = []
    instance.roles = []
    mock_dashboard_cls.return_value = instance

    with (
        patch.dict(
            "sys.modules",
            {"superset.models.dashboard": MagicMock(Dashboard=mock_dashboard_cls)},
        ),
        patch("superset.commands.dashboard.create.feature_flag_manager") as mock_ffm,
        patch(
            "superset.commands.dashboard.create.add_implicit_tags_after_insert",
            new_callable=AsyncMock,
        ) as mock_tags,
    ):
        mock_ffm.is_feature_enabled.return_value = True
        await cmd.run()

    mock_tags.assert_awaited_once_with(mock_dao.session, "dashboard", instance.id, [])


# ---------------------------------------------------------------------------
# MEDIUM fix: export_related=False suppresses chart/dataset/database YAMLs
# Original: superset_old/commands/dashboard/export.py:187 gates ALL related
# emissions (charts, tags, themes, datasets) inside ``if export_related:``.
# When export_related=False (full-assets bundle via AsyncFullAssetManager),
# the dashboard exporter must emit ONLY the dashboard YAML.
# ---------------------------------------------------------------------------


async def test_export_dashboards_export_related_false_suppresses_chart_yaml(
    mock_dao, mock_dashboard
):
    """When export_related=False, _export_single must NOT emit chart YAMLs.

    The original ExportDashboardsCommand._export gates the entire chart
    sub-export inside ``if export_related:``
    (superset_old/commands/dashboard/export.py:187-193).
    When the full-assets manager calls with export_related=False, only the
    dashboard YAML must appear in the output.
    """
    chart = MagicMock()
    chart.id = 10
    chart.slice_name = "Test Chart"
    chart.uuid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    chart.table = None
    chart.tags = []
    chart.export_to_dict.return_value = {
        "slice_name": "Test Chart",
        "viz_type": "table",
    }
    mock_dashboard.slices = [chart]
    mock_dashboard.export_to_dict.return_value = {
        "dashboard_title": "Dash With Charts",
    }
    _exec_returns(mock_dao, one=mock_dashboard)

    cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao, export_related=False)

    with patch("superset.commands.dashboard.export.feature_flag_manager") as mock_ffm:
        mock_ffm.is_feature_enabled.return_value = False
        files = await cmd._export_single(1)  # noqa: SLF001

    file_names = [f for f, _ in files]
    chart_files = [f for f in file_names if f.startswith("charts/")]
    assert not chart_files, (
        "export_related=False must suppress chart YAMLs (1:1 with original "
        "superset_old/commands/dashboard/export.py:187 ``if export_related:`` guard)"
    )
    # Only the dashboard YAML must be present.
    assert len(file_names) == 1
    assert file_names[0].startswith("dashboards/")


# ---------------------------------------------------------------------------
# MEDIUM fix: chart YAMLs inside dashboard bundles must NOT carry a `tags` key
# Original: ExportDashboardsCommand calls ExportChartsCommand.disable_tag_export()
# before the chart sub-export, so chart YAMLs in a dashboard bundle have no
# ``tags`` key (superset_old/commands/dashboard/export.py:191, chart/export.py:84).
# The import-time ``import_tag`` is only invoked when a ``tags`` key exists in a
# chart config (superset_old/commands/dashboard/importers/v1/__init__.py:152-157),
# so having the key changes import behaviour (creates tag associations).
# ---------------------------------------------------------------------------


async def test_export_dashboards_chart_yaml_has_tags_key(mock_dao, mock_dashboard):
    """Chart YAMLs produced inside a dashboard bundle MUST have a ``tags`` key.

    The original superset_old/commands/chart/export.py:77-80 unconditionally adds
    ``payload["tags"]`` inside ``_file_content`` when TAGGING_SYSTEM is enabled.
    ``disable_tag_export()`` only suppresses the separate ``tags.yaml`` yield,
    not the inline tags key (which is required by the importer to restore tags).
    """
    from superset.models.tags import TagType

    tag = MagicMock()
    tag.name = "my-chart-tag"
    tag.description = "chart tag"
    tag.type = TagType.custom

    chart = MagicMock()
    chart.id = 20
    chart.slice_name = "Tagged Chart"
    chart.uuid = "cccccccc-dddd-eeee-ffff-000000000000"
    chart.table = None
    chart.tags = [tag]
    chart.export_to_dict.return_value = {
        "slice_name": "Tagged Chart",
        "viz_type": "bar",
    }

    mock_dashboard.slices = [chart]
    mock_dashboard.export_to_dict.return_value = {
        "dashboard_title": "Dash With Tagged Charts",
    }
    _exec_returns(mock_dao, one=mock_dashboard)

    cmd = ExportDashboardsCommand(model_ids=[1], dao=mock_dao, export_related=True)

    with patch("superset.commands.dashboard.export.feature_flag_manager") as mock_ffm:
        mock_ffm.is_feature_enabled.return_value = True
        files = await cmd._export_single(1)  # noqa: SLF001

    chart_files = [
        (name, content) for name, content in files if name.startswith("charts/")
    ]
    assert chart_files, "export_related=True must produce at least one chart YAML"
    for name, content in chart_files:
        import yaml as _yaml

        parsed = _yaml.safe_load(content)
        assert "tags" in parsed, (
            f"Chart YAML {name!r} must have a ``tags`` key inside a dashboard "
            "bundle so the importer restores them."
        )
