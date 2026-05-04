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
"""Unit tests for annotation commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.annotation import (
    BulkDeleteAnnotationCommand,
    CreateAnnotationCommand,
    DeleteAnnotationCommand,
    UpdateAnnotationCommand,
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
def mock_layer_dao():
    dao = AsyncMock()
    return dao


@pytest.fixture
def mock_annotation():
    ann = MagicMock()
    ann.id = 10
    ann.short_descr = "Test annotation"
    ann.long_descr = "Long description"
    ann.start_dttm = None
    ann.end_dttm = None
    ann.layer_id = 1
    ann.json_metadata = ""
    ann.created_on = None
    ann.changed_on = None
    return ann


@pytest.fixture
def mock_layer():
    layer = MagicMock()
    layer.id = 1
    layer.name = "Test Layer"
    return layer


# ---------------------------------------------------------------------------
# CreateAnnotationCommand
# ---------------------------------------------------------------------------


async def test_create_annotation_layer_not_found(mock_dao, mock_layer_dao):
    mock_layer_dao.find_by_id = AsyncMock(return_value=None)
    cmd = CreateAnnotationCommand(
        dao=mock_dao,
        layer_dao=mock_layer_dao,
        layer_pk=999,
        data={"short_descr": "test"},
    )
    with pytest.raises(ObjectNotFoundError, match="AnnotationLayer"):
        await cmd.validate()


async def test_create_annotation_short_descr_required(
    mock_dao, mock_layer_dao, mock_layer
):
    mock_layer_dao.find_by_id = AsyncMock(return_value=mock_layer)
    cmd = CreateAnnotationCommand(
        dao=mock_dao,
        layer_dao=mock_layer_dao,
        layer_pk=1,
        data={},
    )
    with pytest.raises(CommandInvalidError, match="short_descr"):
        await cmd.validate()


async def test_create_annotation_empty_short_descr(
    mock_dao, mock_layer_dao, mock_layer
):
    mock_layer_dao.find_by_id = AsyncMock(return_value=mock_layer)
    cmd = CreateAnnotationCommand(
        dao=mock_dao,
        layer_dao=mock_layer_dao,
        layer_pk=1,
        data={"short_descr": "   "},
    )
    with pytest.raises(CommandInvalidError, match="short_descr"):
        await cmd.validate()


async def test_create_annotation_success(
    mock_dao, mock_layer_dao, mock_layer, mock_annotation
):
    mock_layer_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.create = AsyncMock(return_value=mock_annotation)
    cmd = CreateAnnotationCommand(
        dao=mock_dao,
        layer_dao=mock_layer_dao,
        layer_pk=1,
        data={"short_descr": "test annotation"},
    )
    result = await cmd.execute()
    assert result.id == 10
    assert result.layer_id == 1
    mock_dao.create.assert_awaited_once()
    # Verify layer_id was injected into data
    call_args = mock_dao.create.call_args[0][0]
    assert call_args["layer_id"] == 1


# ---------------------------------------------------------------------------
# UpdateAnnotationCommand
# ---------------------------------------------------------------------------


async def test_update_annotation_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateAnnotationCommand(dao=mock_dao, pk=999, data={"short_descr": "updated"})
    with pytest.raises(ObjectNotFoundError, match="Annotation"):
        await cmd.validate()


async def test_update_annotation_success(mock_dao, mock_annotation):
    mock_dao.find_by_id = AsyncMock(return_value=mock_annotation)
    mock_dao.update = AsyncMock(return_value=mock_annotation)
    cmd = UpdateAnnotationCommand(dao=mock_dao, pk=10, data={"short_descr": "updated"})
    result = await cmd.execute()
    assert result.id == 10
    mock_dao.update.assert_awaited_once_with(
        mock_annotation, {"short_descr": "updated"}
    )
    mock_dao.session.flush.assert_awaited()


# ---------------------------------------------------------------------------
# DeleteAnnotationCommand
# ---------------------------------------------------------------------------


async def test_delete_annotation_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteAnnotationCommand(dao=mock_dao, pk=999)
    with pytest.raises(ObjectNotFoundError, match="Annotation"):
        await cmd.validate()


async def test_delete_annotation_success(mock_dao, mock_annotation):
    mock_dao.find_by_id = AsyncMock(return_value=mock_annotation)
    mock_dao.delete = AsyncMock()
    cmd = DeleteAnnotationCommand(dao=mock_dao, pk=10)
    await cmd.execute()
    mock_dao.delete.assert_awaited_once_with([mock_annotation])
    mock_dao.session.flush.assert_awaited()


# ---------------------------------------------------------------------------
# BulkDeleteAnnotationCommand
# ---------------------------------------------------------------------------


async def test_bulk_delete_annotations_empty_ids(mock_dao):
    cmd = BulkDeleteAnnotationCommand(dao=mock_dao, ids=[])
    with pytest.raises(CommandInvalidError, match="No annotation IDs"):
        await cmd.validate()


async def test_bulk_delete_annotations_missing(mock_dao, mock_annotation):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_annotation])
    cmd = BulkDeleteAnnotationCommand(dao=mock_dao, ids=[10, 20])
    with pytest.raises(ObjectNotFoundError, match="Annotation"):
        await cmd.validate()


async def test_bulk_delete_annotations_success(mock_dao, mock_annotation):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_annotation])
    mock_dao.delete = AsyncMock()
    cmd = BulkDeleteAnnotationCommand(dao=mock_dao, ids=[10])
    await cmd.execute()
    mock_dao.delete.assert_awaited_once_with([mock_annotation])
    mock_dao.session.flush.assert_awaited()
