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
"""Tests for Theme commands and controller."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.theme import (
    CreateThemeCommand,
    DeleteThemeCommand,
    ImportThemesCommand,
    SetSystemDefaultCommand,
    UnsetSystemDefaultCommand,
    UpdateThemeCommand,
)
from superset.exceptions import (
    CommandInvalidError,
    ForbiddenError,
    ObjectNotFoundError,
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
def mock_theme():
    theme = MagicMock()
    theme.id = 1
    theme.theme_name = "Dark Theme"
    theme.css = "body { background: #000; }"
    theme.json_metadata = "{}"
    theme.description = "A dark theme"
    theme.is_system_default = False
    theme.is_system = False
    theme.is_system_dark = False
    return theme


# ---------------------------------------------------------------------------
# CreateThemeCommand
# ---------------------------------------------------------------------------


async def test_create_validates_empty_name(mock_dao):
    cmd = CreateThemeCommand(dao=mock_dao, data={"theme_name": ""})
    with pytest.raises(CommandInvalidError, match="theme_name"):
        await cmd.validate()


async def test_create_validates_missing_name(mock_dao):
    cmd = CreateThemeCommand(dao=mock_dao, data={})
    with pytest.raises(CommandInvalidError, match="theme_name"):
        await cmd.validate()


async def test_create_validates_whitespace_name(mock_dao):
    cmd = CreateThemeCommand(dao=mock_dao, data={"theme_name": "   "})
    with pytest.raises(CommandInvalidError, match="theme_name"):
        await cmd.validate()


async def test_create_success(mock_dao, mock_theme):
    mock_dao.create.return_value = mock_theme
    # ThemeBaseSchema requires json_data (a parseable JSON string); there is no
    # ``css`` field.
    data = {"theme_name": "Dark Theme", "json_data": "{}"}
    cmd = CreateThemeCommand(dao=mock_dao, data=data)
    result = await cmd.execute()
    assert result.id == 1
    mock_dao.create.assert_awaited_once_with(data)
    mock_dao.session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# UpdateThemeCommand
# ---------------------------------------------------------------------------


async def test_update_not_found(mock_dao):
    mock_dao.find_by_id.return_value = None
    cmd = UpdateThemeCommand(dao=mock_dao, pk=999, data={"theme_name": "New"})
    with pytest.raises(ObjectNotFoundError, match="Theme"):
        await cmd.execute()


async def test_update_success(mock_dao, mock_theme):
    mock_dao.find_by_id.return_value = mock_theme
    mock_dao.update.return_value = mock_theme
    data = {"theme_name": "Updated Theme"}
    cmd = UpdateThemeCommand(dao=mock_dao, pk=1, data=data)
    result = await cmd.execute()
    assert result.id == 1
    mock_dao.update.assert_awaited_once_with(mock_theme, data)


async def test_update_system_theme_blocked(mock_dao, mock_theme):
    """System themes must not be modifiable. Raises ``ForbiddenError``
    (HTTP 403), consistent with ``_validate_theme_deletable``."""
    mock_theme.is_system = True
    mock_dao.find_by_id.return_value = mock_theme
    cmd = UpdateThemeCommand(dao=mock_dao, pk=1, data={"theme_name": "Hacked"})
    with pytest.raises(ForbiddenError, match="System themes cannot be modified"):
        await cmd.execute()
    # DAO update must NOT have been called when the guard fires.
    mock_dao.update.assert_not_awaited()


# ---------------------------------------------------------------------------
# DeleteThemeCommand
# ---------------------------------------------------------------------------


async def test_delete_not_found(mock_dao):
    mock_dao.find_by_id.return_value = None
    cmd = DeleteThemeCommand(dao=mock_dao, pk=999)
    with pytest.raises(ObjectNotFoundError, match="Theme"):
        await cmd.execute()


async def test_delete_system_default_blocked(mock_dao, mock_theme):
    mock_theme.is_system_default = True
    mock_dao.find_by_id.return_value = mock_theme
    cmd = DeleteThemeCommand(dao=mock_dao, pk=1)
    # is_system_default/dark in use → 422 CommandInvalidError (1:1 upstream
    # SystemThemeInUseError).
    with pytest.raises(CommandInvalidError, match="system default"):
        await cmd.execute()


async def test_delete_success(mock_dao, mock_theme):
    mock_dao.find_by_id.return_value = mock_theme
    cmd = DeleteThemeCommand(dao=mock_dao, pk=1)
    await cmd.execute()
    mock_dao.delete.assert_awaited_once_with([mock_theme])
    mock_dao.session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# SetSystemDefaultCommand
# ---------------------------------------------------------------------------


async def test_set_default_not_found(mock_dao):
    mock_dao.find_by_id.return_value = None
    cmd = SetSystemDefaultCommand(dao=mock_dao, pk=999)
    with pytest.raises(ObjectNotFoundError, match="Theme"):
        await cmd.execute()


async def test_set_default_unsets_previous(mock_dao, mock_theme):
    old_default = MagicMock()
    old_default.id = 2
    old_default.is_system_default = True
    mock_dao.find_by_id.return_value = mock_theme
    mock_dao.find_system_default.return_value = old_default

    cmd = SetSystemDefaultCommand(dao=mock_dao, pk=1)
    result = await cmd.execute()

    # The command issues a bulk SA UPDATE instead of mutating the ORM object in Python.
    # Verify the new default is set on the in-memory model and that the session
    # executed the bulk clear query.
    assert result.is_system_default is True
    mock_dao.session.execute.assert_awaited()


async def test_set_default_no_previous(mock_dao, mock_theme):
    mock_dao.find_by_id.return_value = mock_theme
    mock_dao.find_system_default.return_value = None

    cmd = SetSystemDefaultCommand(dao=mock_dao, pk=1)
    result = await cmd.execute()

    assert result.is_system_default is True


async def test_set_default_same_theme(mock_dao, mock_theme):
    """Setting the same theme as default should be idempotent."""
    mock_theme.is_system_default = True
    mock_dao.find_by_id.return_value = mock_theme
    mock_dao.find_system_default.return_value = mock_theme

    cmd = SetSystemDefaultCommand(dao=mock_dao, pk=1)
    result = await cmd.execute()

    assert result.is_system_default is True


# ---------------------------------------------------------------------------
# UnsetSystemDefaultCommand
# ---------------------------------------------------------------------------


async def test_unset_default_success(mock_dao):
    current_default = MagicMock()
    current_default.is_system_default = True
    mock_dao.find_system_default.return_value = current_default

    cmd = UnsetSystemDefaultCommand(dao=mock_dao)
    await cmd.execute()

    # Command issues a bulk SA UPDATE — the in-memory MagicMock attribute is NOT
    # mutated by the DB-level UPDATE; verify session executed the clear query.
    mock_dao.session.execute.assert_awaited()
    mock_dao.session.flush.assert_awaited_once()


async def test_unset_default_no_current(mock_dao):
    """Unset when there is no current default should be a no-op."""
    mock_dao.find_system_default.return_value = None

    cmd = UnsetSystemDefaultCommand(dao=mock_dao)
    await cmd.execute()
    # No error raised, flush still called (no-op scenario)


# ---------------------------------------------------------------------------
# ImportThemesCommand — per-config schema validation (finding fix)
# ImportV1ThemeSchema requires theme_name, json_data, uuid, and version
# as required fields.  Missing/invalid fields must raise CommandInvalidError
# (HTTP 422), not silently insert bad rows or crash with HTTP 500 in
# BinaryUUID.process_bind_param.
# ---------------------------------------------------------------------------

_VALID_UUID = "12345678-1234-5678-1234-567812345678"
_VALID_CONFIG = {
    "theme_name": "My Theme",
    "json_data": "{}",
    "uuid": _VALID_UUID,
    "version": "1.0.0",
}


@pytest.fixture
def import_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.flush = AsyncMock()
    dao.find_by_uuid = AsyncMock(return_value=None)
    dao.create = AsyncMock()
    dao.update = AsyncMock()
    return dao


async def test_import_missing_uuid_raises_422(import_dao):
    """Missing uuid must raise CommandInvalidError (HTTP 422), not produce a
    random-UUID row."""
    config = {k: v for k, v in _VALID_CONFIG.items() if k != "uuid"}
    cmd = ImportThemesCommand(dao=import_dao, contents={"themes/t.yaml": config})
    with pytest.raises(CommandInvalidError, match="uuid"):
        await cmd.validate()


async def test_import_invalid_uuid_raises_422(import_dao):
    """An invalid UUID string must raise CommandInvalidError (HTTP 422)
    instead of reaching BinaryUUID.process_bind_param → ValueError → 500."""
    config = {**_VALID_CONFIG, "uuid": "not-a-uuid"}
    cmd = ImportThemesCommand(dao=import_dao, contents={"themes/t.yaml": config})
    with pytest.raises(CommandInvalidError, match="uuid"):
        await cmd.validate()


async def test_import_missing_theme_name_raises_422(import_dao):
    """Missing theme_name must raise CommandInvalidError (HTTP 422), not
    silently create a NULL-named theme."""
    config = {k: v for k, v in _VALID_CONFIG.items() if k != "theme_name"}
    cmd = ImportThemesCommand(dao=import_dao, contents={"themes/t.yaml": config})
    with pytest.raises(CommandInvalidError, match="theme_name"):
        await cmd.validate()


async def test_import_missing_version_raises_422(import_dao):
    """Missing version must raise CommandInvalidError (HTTP 422)."""
    config = {k: v for k, v in _VALID_CONFIG.items() if k != "version"}
    cmd = ImportThemesCommand(dao=import_dao, contents={"themes/t.yaml": config})
    with pytest.raises(CommandInvalidError, match="version"):
        await cmd.validate()


async def test_import_missing_json_data_raises_422(import_dao):
    """Missing json_data must raise CommandInvalidError (HTTP 422)."""
    config = {k: v for k, v in _VALID_CONFIG.items() if k != "json_data"}
    cmd = ImportThemesCommand(dao=import_dao, contents={"themes/t.yaml": config})
    with pytest.raises(CommandInvalidError, match="json_data"):
        await cmd.validate()


async def test_import_valid_config_passes_validate(import_dao):
    """A fully valid config must pass validate() without raising."""
    cmd = ImportThemesCommand(
        dao=import_dao, contents={"themes/t.yaml": dict(_VALID_CONFIG)}
    )
    await cmd.validate()  # must not raise
    # overwrite check: find_by_uuid called once to check existing
    import_dao.find_by_uuid.assert_awaited_once()


async def test_import_overwrite_false_raises_for_existing_uuid(import_dao):
    """When overwrite=False and a theme with the given UUID already exists,
    validate() must raise CommandInvalidError."""
    existing_theme = MagicMock()
    import_dao.find_by_uuid.return_value = existing_theme
    cmd = ImportThemesCommand(
        dao=import_dao,
        contents={"themes/t.yaml": dict(_VALID_CONFIG)},
        overwrite=False,
    )
    with pytest.raises(CommandInvalidError, match="already exists"):
        await cmd.validate()


async def test_import_overwrite_true_skips_exists_check(import_dao):
    """When overwrite=True, validate() must NOT raise even if the UUID exists."""
    existing_theme = MagicMock()
    import_dao.find_by_uuid.return_value = existing_theme
    cmd = ImportThemesCommand(
        dao=import_dao,
        contents={"themes/t.yaml": dict(_VALID_CONFIG)},
        overwrite=True,
    )
    await cmd.validate()  # must not raise
    # No uuid existence check happens when overwrite=True
    import_dao.find_by_uuid.assert_not_awaited()


async def test_import_non_theme_files_are_skipped(import_dao):
    """Files not under 'themes/' prefix must be silently ignored — metadata.yaml
    etc. are not theme configs."""
    contents = {
        "metadata.yaml": {"version": "1.0.0", "type": "Theme"},
        # bad config but under a different prefix — must not trigger 422
        "charts/bad.yaml": {"no_uuid": True},
    }
    cmd = ImportThemesCommand(dao=import_dao, contents=contents)
    await cmd.validate()  # must not raise


# ---------------------------------------------------------------------------
# Controller class attributes
# ---------------------------------------------------------------------------


def test_controller_path():
    from superset.controllers.theme import ThemeController

    assert ThemeController.path == "/api/v1/theme"
    assert "Themes" in ThemeController.tags


def test_controller_has_dependencies():
    from superset.controllers.theme import ThemeController

    assert "dao" in ThemeController.dependencies
    assert "rison_params" in ThemeController.dependencies


# ---------------------------------------------------------------------------
# related() handler — EXTRA_RELATED_QUERY_FILTERS["user"] scoping
# base_related_field_filters maps ONLY "changed_by" → BaseFilterRelatedUsers.
# "created_by" has no entry, so EXTRA_RELATED_QUERY_FILTERS["user"] must NOT
# be passed as query_hook for created_by.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column_name,hook_should_be_passed",
    [
        ("changed_by", True),  # base_related_field_filters maps this → hook applied
        ("created_by", False),  # no entry in base_related_field_filters → no hook
    ],
)
async def test_related_extra_query_filter_scoped_to_changed_by(
    column_name, hook_should_be_passed
):
    """EXTRA_RELATED_QUERY_FILTERS["user"] hook must only be forwarded to
    get_related_payload for changed_by, never for created_by.

    base_related_field_filters only registers BaseFilterRelatedUsers for
    changed_by; created_by has no entry, so the hook must not be passed.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from superset.controllers.theme import ThemeController

    user_hook = MagicMock(side_effect=lambda q: q)

    # Build a fake State that exposes settings with the hook configured.
    settings_mock = MagicMock()
    settings_mock.extra_related_query_filters = {"user": user_hook}
    settings_mock.exclude_users_from_lists = None
    state_mock = MagicMock()
    state_mock.settings = settings_mock

    dao_mock = AsyncMock()
    security_manager_mock = MagicMock()
    security_manager_mock.get_exclude_users_from_lists = MagicMock(return_value=[])

    captured: dict = {}

    async def fake_get_related_payload(**kwargs):  # type: ignore[misc]
        captured.update(kwargs)
        return {"count": 0, "result": []}

    controller = ThemeController.__new__(ThemeController)
    # ThemeController.related is a Litestar HTTPRouteHandler; access the
    # underlying coroutine via .fn to call it directly in unit tests.
    related_fn = ThemeController.related.fn

    with patch(
        "superset.controllers.theme.get_related_payload",
        side_effect=fake_get_related_payload,
    ):
        await related_fn(
            controller,
            column_name=column_name,
            dao=dao_mock,
            rison_params=None,
            state=state_mock,
            security_manager=security_manager_mock,
        )

    if hook_should_be_passed:
        assert captured.get("query_hook") is user_hook, (
            f"Expected query_hook to be the user_extra_filter for {column_name!r}"
        )
    else:
        assert captured.get("query_hook") is None, (
            f"query_hook must be None for {column_name!r} — "
            'EXTRA_RELATED_QUERY_FILTERS["user"] is not registered for created_by'
        )
