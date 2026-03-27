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
"""Tests for Tag commands and controller."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.tag import (
    BulkCreateTagCommand,
    BulkDeleteTagCommand,
    CreateTagCommand,
    DeleteTagCommand,
    UpdateTagCommand,
)
from superset.exceptions import CommandInvalidError, ObjectNotFoundError


@pytest.fixture
def mock_dao() -> AsyncMock:
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    return dao


async def test_create_tag_validates_name(mock_dao: AsyncMock) -> None:
    cmd = CreateTagCommand(dao=mock_dao, data={"name": ""})
    with pytest.raises(CommandInvalidError, match="name"):
        await cmd.validate()


async def test_create_tag_validates_whitespace_name(mock_dao: AsyncMock) -> None:
    cmd = CreateTagCommand(dao=mock_dao, data={"name": "   "})
    with pytest.raises(CommandInvalidError, match="name"):
        await cmd.validate()


async def test_create_tag_validates_missing_name(mock_dao: AsyncMock) -> None:
    cmd = CreateTagCommand(dao=mock_dao, data={})
    with pytest.raises(CommandInvalidError, match="name"):
        await cmd.validate()


async def test_create_tag_success(mock_dao: AsyncMock) -> None:
    tag = MagicMock()
    tag.id = 1
    tag.name = "TestTag"
    mock_dao.get_by_name.return_value = tag
    cmd = CreateTagCommand(dao=mock_dao, data={"name": "TestTag"})
    await cmd.validate()
    result = await cmd.run()
    assert result.name == "TestTag"
    mock_dao.get_by_name.assert_awaited_once_with("TestTag", "custom")


async def test_create_tag_with_description(mock_dao: AsyncMock) -> None:
    tag = MagicMock()
    tag.id = 1
    tag.name = "MyTag"
    mock_dao.get_by_name.return_value = tag
    cmd = CreateTagCommand(
        dao=mock_dao, data={"name": "MyTag", "description": "A test tag"}
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.description == "A test tag"


async def test_create_tag_with_objects_to_tag(mock_dao: AsyncMock) -> None:
    tag = MagicMock()
    tag.id = 1
    tag.name = "MyTag"
    mock_dao.get_by_name.return_value = tag
    cmd = CreateTagCommand(
        dao=mock_dao,
        data={
            "name": "MyTag",
            "objects_to_tag": [
                {"object_type": "chart", "object_id": 10},
                {"object_type": "dashboard", "object_id": 20},
            ],
        },
    )
    await cmd.validate()
    await cmd.run()
    assert mock_dao.create_custom_tagged_objects.await_count == 2


async def test_update_tag_not_found(mock_dao: AsyncMock) -> None:
    mock_dao.find_by_id.return_value = None
    cmd = UpdateTagCommand(dao=mock_dao, pk=999, data={"name": "New"})
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_tag_success(mock_dao: AsyncMock) -> None:
    item = MagicMock()
    item.id = 1
    item.name = "OldName"
    mock_dao.find_by_id.return_value = item
    updated = MagicMock()
    updated.name = "NewName"
    mock_dao.update.return_value = updated
    cmd = UpdateTagCommand(dao=mock_dao, pk=1, data={"name": "NewName"})
    await cmd.validate()
    result = await cmd.run()
    assert result.name == "NewName"
    mock_dao.update.assert_awaited_once_with(item, {"name": "NewName"})


async def test_delete_tag_not_found(mock_dao: AsyncMock) -> None:
    mock_dao.find_by_id.return_value = None
    cmd = DeleteTagCommand(dao=mock_dao, pk=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_tag_success(mock_dao: AsyncMock) -> None:
    item = MagicMock()
    mock_dao.find_by_id.return_value = item
    cmd = DeleteTagCommand(dao=mock_dao, pk=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([item])


async def test_bulk_delete_empty(mock_dao: AsyncMock) -> None:
    cmd = BulkDeleteTagCommand(dao=mock_dao, ids=[])
    with pytest.raises(CommandInvalidError, match="No IDs"):
        await cmd.validate()


async def test_bulk_delete_success(mock_dao: AsyncMock) -> None:
    mock_dao.bulk_delete.return_value = 3
    cmd = BulkDeleteTagCommand(dao=mock_dao, ids=[1, 2, 3])
    await cmd.validate()
    result = await cmd.run()
    assert result == 3
    mock_dao.bulk_delete.assert_awaited_once_with([1, 2, 3])


async def test_bulk_create_validates_names(mock_dao: AsyncMock) -> None:
    cmd = BulkCreateTagCommand(dao=mock_dao, tags_data=[{"name": ""}])
    with pytest.raises(CommandInvalidError, match="name"):
        await cmd.validate()


async def test_bulk_create_validates_all_names(mock_dao: AsyncMock) -> None:
    cmd = BulkCreateTagCommand(
        dao=mock_dao,
        tags_data=[{"name": "Good"}, {"name": "  "}],
    )
    with pytest.raises(CommandInvalidError, match="name"):
        await cmd.validate()


async def test_bulk_create_success(mock_dao: AsyncMock) -> None:
    tag1 = MagicMock()
    tag1.id = 1
    tag1.name = "Tag1"
    tag2 = MagicMock()
    tag2.id = 2
    tag2.name = "Tag2"
    mock_dao.get_by_name.side_effect = [tag1, tag2]
    cmd = BulkCreateTagCommand(
        dao=mock_dao,
        tags_data=[{"name": "Tag1"}, {"name": "Tag2"}],
    )
    await cmd.validate()
    results = await cmd.run()
    assert len(results) == 2
    assert results[0].name == "Tag1"
    assert results[1].name == "Tag2"
