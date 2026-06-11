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

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.css_template import (
    BulkDeleteCssTemplateCommand,
    CreateCssTemplateCommand,
    DeleteCssTemplateCommand,
    UpdateCssTemplateCommand,
)
from superset.exceptions import CommandInvalidError, ObjectNotFoundError


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
    return dao


@pytest.fixture
def mock_template():
    template = MagicMock()
    template.id = 1
    template.template_name = "Test Template"
    template.css = "body { color: red; }"
    template.created_on = "2026-01-01T00:00:00"
    template.changed_on = "2026-01-01T00:00:00"
    return template


# ---------------------------------------------------------------------------
# CreateCssTemplateCommand
# ---------------------------------------------------------------------------


class TestCreateCssTemplateCommand:
    async def test_validate_missing_template_name_accepted(self, mock_dao):
        # 1:1 upstream: FAB only marks the field required at the schema
        # layer — the command itself does NOT reject absent/empty names.
        cmd = CreateCssTemplateCommand(dao=mock_dao, data={"css": "body{}"})
        await cmd.validate()  # should not raise

    async def test_validate_empty_template_name_accepted(self, mock_dao):
        cmd = CreateCssTemplateCommand(
            dao=mock_dao, data={"template_name": "  ", "css": "body{}"}
        )
        await cmd.validate()  # should not raise

    async def test_validate_success(self, mock_dao):
        cmd = CreateCssTemplateCommand(
            dao=mock_dao, data={"template_name": "Test", "css": "body{}"}
        )
        await cmd.validate()  # should not raise

    async def test_run_success(self, mock_dao, mock_template):
        mock_dao.create.return_value = mock_template
        cmd = CreateCssTemplateCommand(
            dao=mock_dao, data={"template_name": "Test", "css": "body{}"}
        )
        result = await cmd.run()
        assert result.id == 1
        assert result.template_name == "Test Template"
        mock_dao.create.assert_called_once_with(
            {"template_name": "Test", "css": "body{}"}
        )

    async def test_execute_success(self, mock_dao, mock_template):
        mock_dao.create.return_value = mock_template
        cmd = CreateCssTemplateCommand(
            dao=mock_dao, data={"template_name": "Test", "css": "body{}"}
        )
        result = await cmd.execute()
        assert result.id == 1


# ---------------------------------------------------------------------------
# UpdateCssTemplateCommand
# ---------------------------------------------------------------------------


class TestUpdateCssTemplateCommand:
    async def test_validate_not_found(self, mock_dao):
        mock_dao.find_by_id.return_value = None
        cmd = UpdateCssTemplateCommand(dao=mock_dao, pk=999, data={"css": "new"})
        with pytest.raises(ObjectNotFoundError):
            await cmd.validate()

    async def test_validate_found(self, mock_dao, mock_template):
        mock_dao.find_by_id.return_value = mock_template
        cmd = UpdateCssTemplateCommand(dao=mock_dao, pk=1, data={"css": "new"})
        await cmd.validate()  # should not raise

    async def test_run_success(self, mock_dao, mock_template):
        mock_dao.find_by_id.return_value = mock_template
        mock_dao.update.return_value = mock_template
        cmd = UpdateCssTemplateCommand(dao=mock_dao, pk=1, data={"css": "new"})
        await cmd.validate()
        result = await cmd.run()
        assert result.id == 1
        mock_dao.update.assert_called_once_with(mock_template, {"css": "new"})

    async def test_execute_success(self, mock_dao, mock_template):
        mock_dao.find_by_id.return_value = mock_template
        mock_dao.update.return_value = mock_template
        cmd = UpdateCssTemplateCommand(dao=mock_dao, pk=1, data={"css": "new"})
        result = await cmd.execute()
        assert result.id == 1


# ---------------------------------------------------------------------------
# DeleteCssTemplateCommand
# ---------------------------------------------------------------------------


class TestDeleteCssTemplateCommand:
    async def test_validate_not_found(self, mock_dao):
        mock_dao.find_by_id.return_value = None
        cmd = DeleteCssTemplateCommand(dao=mock_dao, pk=999)
        with pytest.raises(ObjectNotFoundError):
            await cmd.validate()

    async def test_validate_found(self, mock_dao, mock_template):
        mock_dao.find_by_id.return_value = mock_template
        cmd = DeleteCssTemplateCommand(dao=mock_dao, pk=1)
        await cmd.validate()  # should not raise

    async def test_run_success(self, mock_dao, mock_template):
        mock_dao.find_by_id.return_value = mock_template
        mock_dao.delete.return_value = None
        cmd = DeleteCssTemplateCommand(dao=mock_dao, pk=1)
        await cmd.validate()
        await cmd.run()
        mock_dao.delete.assert_called_once_with([mock_template])
        mock_dao.session.flush.assert_called_once()

    async def test_execute_success(self, mock_dao, mock_template):
        mock_dao.find_by_id.return_value = mock_template
        mock_dao.delete.return_value = None
        cmd = DeleteCssTemplateCommand(dao=mock_dao, pk=1)
        await cmd.execute()
        mock_dao.delete.assert_called_once()


# ---------------------------------------------------------------------------
# BulkDeleteCssTemplateCommand
# ---------------------------------------------------------------------------


class TestBulkDeleteCssTemplateCommand:
    async def test_validate_empty_ids(self, mock_dao):
        cmd = BulkDeleteCssTemplateCommand(dao=mock_dao, ids=[])
        with pytest.raises(CommandInvalidError, match="No CSS template IDs"):
            await cmd.validate()

    async def test_validate_missing_ids(self, mock_dao):
        items = [MagicMock(id=1)]
        mock_dao.find_by_ids.return_value = items
        cmd = BulkDeleteCssTemplateCommand(dao=mock_dao, ids=[1, 2])
        with pytest.raises(ObjectNotFoundError):
            await cmd.validate()

    async def test_validate_all_found(self, mock_dao):
        items = [MagicMock(id=1), MagicMock(id=2)]
        mock_dao.find_by_ids.return_value = items
        cmd = BulkDeleteCssTemplateCommand(dao=mock_dao, ids=[1, 2])
        await cmd.validate()  # should not raise

    async def test_run_success(self, mock_dao):
        items = [MagicMock(id=1), MagicMock(id=2)]
        mock_dao.find_by_ids.return_value = items
        mock_dao.delete.return_value = None
        cmd = BulkDeleteCssTemplateCommand(dao=mock_dao, ids=[1, 2])
        await cmd.validate()
        await cmd.run()
        mock_dao.delete.assert_called_once_with(items)
        mock_dao.session.flush.assert_called_once()

    async def test_execute_success(self, mock_dao):
        items = [MagicMock(id=1), MagicMock(id=2)]
        mock_dao.find_by_ids.return_value = items
        mock_dao.delete.return_value = None
        cmd = BulkDeleteCssTemplateCommand(dao=mock_dao, ids=[1, 2])
        await cmd.execute()
        mock_dao.delete.assert_called_once()
