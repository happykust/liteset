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
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetValidationException,
)
from superset.schemas.tag import AddTagsToObjectProperties, AddTagsToObjectSchema


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
            # ``[object_type, object_id]`` PAIRS — the shape the frontend
            # (TagModal/BulkTagModal) and TagPostSchema use. (The previous
            # dict shape made the command crash with TypeError -> HTTP 500.)
            "objects_to_tag": [
                ["chart", 10],
                ["dashboard", 20],
            ],
        },
    )
    await cmd.validate()
    await cmd.run()
    assert mock_dao.create_custom_tagged_objects.await_count == 2
    mock_dao.create_custom_tagged_objects.assert_any_await(
        object_type="chart", object_id=10, tag_names=["MyTag"]
    )


async def test_create_tag_invalid_object_type_raises(mock_dao: AsyncMock) -> None:
    """An unknown object type is a 4xx CommandInvalidError, not a 500."""
    tag = MagicMock()
    tag.name = "MyTag"
    mock_dao.get_by_name.return_value = tag
    cmd = CreateTagCommand(
        dao=mock_dao,
        data={"name": "MyTag", "objects_to_tag": [["BADTYPE", 1]]},
    )
    with pytest.raises(CommandInvalidError, match="invalid object type"):
        await cmd.run()
    mock_dao.create_custom_tagged_objects.assert_not_awaited()


async def test_create_tag_malformed_object_entry_raises(mock_dao: AsyncMock) -> None:
    """A non-pair objects_to_tag entry is a 4xx, not a 500."""
    tag = MagicMock()
    tag.name = "MyTag"
    mock_dao.get_by_name.return_value = tag
    cmd = CreateTagCommand(
        dao=mock_dao,
        data={"name": "MyTag", "objects_to_tag": [{"object_type": "chart"}]},
    )
    with pytest.raises(CommandInvalidError, match="Invalid objects_to_tag"):
        await cmd.run()


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
    # description is always passed to dao.update (None when absent), mirroring
    # superset_old/commands/tag/update.py:48: ``self._model.description =
    # self._properties.get('description')`` which writes NULL when absent.
    mock_dao.update.assert_awaited_once_with(
        item, {"name": "NewName", "description": None}
    )


async def test_update_tag_clears_description_when_absent(mock_dao: AsyncMock) -> None:
    """PUT body without description must clear the column to NULL (original behaviour).

    superset_old/commands/tag/update.py:48 always executes:
        self._model.description = self._properties.get('description')
    returning None when key is absent, which writes NULL.  The liteset
    controller strips UNSET fields via filter_unset before calling the
    command, so the command must re-inject description=None explicitly.
    """
    item = MagicMock()
    item.id = 5
    item.name = "ExistingTag"
    item.description = "old description"
    mock_dao.find_by_id.return_value = item
    updated = MagicMock()
    updated.id = 5
    updated.name = "ExistingTag"
    updated.description = None
    mock_dao.update.return_value = updated

    # Simulate controller calling filter_unset: description is absent
    cmd = UpdateTagCommand(dao=mock_dao, pk=5, data={"name": "ExistingTag"})
    await cmd.validate()
    result = await cmd.run()

    # dao.update must have received description=None to clear the column
    call_args = mock_dao.update.call_args
    assert call_args is not None
    passed_data: dict = (
        call_args.args[1]
        if len(call_args.args) > 1
        else call_args.kwargs.get("attributes", {})
    )
    assert "description" in passed_data, (
        "description must always be passed to dao.update so the column is "
        "explicitly cleared to NULL when omitted from the PUT body"
    )
    assert passed_data["description"] is None, (
        "description must be None (NULL) when absent from the PUT body, "
        "matching superset_old/commands/tag/update.py:48"
    )
    assert result.description is None


async def test_update_tag_preserves_description_when_provided(
    mock_dao: AsyncMock,
) -> None:
    """PUT body with description must pass that value through to dao.update."""
    item = MagicMock()
    item.id = 7
    item.name = "MyTag"
    mock_dao.find_by_id.return_value = item
    updated = MagicMock()
    updated.description = "new desc"
    mock_dao.update.return_value = updated

    cmd = UpdateTagCommand(
        dao=mock_dao, pk=7, data={"name": "MyTag", "description": "new desc"}
    )
    await cmd.validate()
    result = await cmd.run()

    call_args = mock_dao.update.call_args
    passed_data: dict = (
        call_args.args[1]
        if len(call_args.args) > 1
        else call_args.kwargs.get("attributes", {})
    )
    assert passed_data.get("description") == "new desc"
    assert result.description == "new desc"


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
    # 1:1 with original DeleteTagsCommand: deletes by tag *name*.
    cmd = BulkDeleteTagCommand(dao=mock_dao, tag_names=[])
    with pytest.raises(CommandInvalidError, match="No tag names"):
        await cmd.validate()


async def test_bulk_delete_success(mock_dao: AsyncMock) -> None:
    mock_dao.find_by_name.return_value = MagicMock()
    cmd = BulkDeleteTagCommand(dao=mock_dao, tag_names=["a", "b", "c"])
    await cmd.validate()
    result = await cmd.run()
    assert result == 3
    mock_dao.delete_tags.assert_awaited_once_with(["a", "b", "c"])


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
    # Returns the upstream ``{objects_tagged, objects_skipped}`` payload (the
    # shape BulkTagModal consumes), NOT a list of tag objects. With no
    # ``objects_to_tag`` both lists are empty; the tags are still created.
    result = await cmd.run()
    assert result == {"objects_tagged": [], "objects_skipped": []}
    assert mock_dao.get_by_name.call_count == 2


# ---------------------------------------------------------------------------
# add_objects: object_id == 0 guard
# ---------------------------------------------------------------------------


async def test_add_objects_zero_object_id_raises_422(mock_dao: AsyncMock) -> None:
    """POST /{object_type}/{object_id}/ with object_id==0 must return 422.

    1:1 with superset_old/commands/tag/create.py:55-64:
    CreateCustomTagCommand.validate() appends TagCreateFailedError when
    object_id==0 and raises TagInvalidError → api.py:407-408 returns 422
    "Invalid tag".  The liteset controller must reproduce this guard so that
    a client sending e.g. POST /api/v1/tag/2/0/ is rejected rather than
    silently persisting TaggedObject(object_id=0).
    """
    from superset.controllers.tag import TagController

    ctrl = TagController.__new__(TagController)
    # Access the underlying async function via .fn (Litestar route handler
    # attribute) so we can call it directly without the full ASGI machinery.
    add_objects_fn = TagController.add_objects.fn

    schema = AddTagsToObjectSchema(
        properties=AddTagsToObjectProperties(tags=["some-tag"])
    )
    current_user = MagicMock(id=1)

    with pytest.raises(SupersetValidationException, match="Invalid tag"):
        await add_objects_fn(
            ctrl,
            object_type=2,  # ObjectType.chart
            object_id=0,
            data=schema,
            dao=mock_dao,
            current_user=current_user,
        )

    # DAO must NOT be called — the guard fires before any persistence.
    mock_dao.create_custom_tagged_objects.assert_not_awaited()


async def test_add_objects_nonzero_object_id_proceeds(mock_dao: AsyncMock) -> None:
    """POST /{object_type}/{object_id}/ with object_id > 0 calls the DAO normally."""
    from superset.controllers.tag import TagController

    ctrl = TagController.__new__(TagController)
    add_objects_fn = TagController.add_objects.fn

    schema = AddTagsToObjectSchema(
        properties=AddTagsToObjectProperties(tags=["some-tag"])
    )
    current_user = MagicMock(id=1)

    # event_logger.alog_with_context is a real coroutine; mock it out.
    from unittest.mock import patch

    with patch(
        "superset.controllers.tag.event_logger.alog_with_context",
        new=AsyncMock(),
    ):
        result = await add_objects_fn(
            ctrl,
            object_type=2,  # ObjectType.chart
            object_id=42,
            data=schema,
            dao=mock_dao,
            current_user=current_user,
        )

    mock_dao.create_custom_tagged_objects.assert_awaited_once_with(
        object_type="chart",
        object_id=42,
        tag_names=["some-tag"],
    )
    assert result == {"message": "OK"}
