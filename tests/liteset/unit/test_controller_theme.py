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

from liteset.commands.theme import (
    CreateThemeCommand,
    DeleteThemeCommand,
    SetSystemDefaultCommand,
    UnsetSystemDefaultCommand,
    UpdateThemeCommand,
)
from liteset.exceptions import (
    CommandInvalidError,
    DeleteFailedError,
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
    data = {"theme_name": "Dark Theme", "css": "body {}"}
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
    with pytest.raises(DeleteFailedError, match="system default"):
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

    assert result.is_system_default is True
    assert old_default.is_system_default is False


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

    assert current_default.is_system_default is False
    mock_dao.session.flush.assert_awaited_once()


async def test_unset_default_no_current(mock_dao):
    """Unset when there is no current default should be a no-op."""
    mock_dao.find_system_default.return_value = None

    cmd = UnsetSystemDefaultCommand(dao=mock_dao)
    await cmd.execute()
    # No error raised, flush still called (no-op scenario)


# ---------------------------------------------------------------------------
# Controller class attributes
# ---------------------------------------------------------------------------


def test_controller_path():
    from liteset.controllers.theme import ThemeController

    assert ThemeController.path == "/api/v1/theme"
    assert "Themes" in ThemeController.tags


def test_controller_has_dependencies():
    from liteset.controllers.theme import ThemeController

    assert "dao" in ThemeController.dependencies
    assert "rison_params" in ThemeController.dependencies
