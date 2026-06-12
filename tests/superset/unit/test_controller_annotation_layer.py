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
"""Unit tests for annotation layer commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.annotation_layer.create import CreateAnnotationLayerCommand
from superset.commands.annotation_layer.delete import (
    BulkDeleteAnnotationLayerCommand,
    DeleteAnnotationLayerCommand,
)
from superset.commands.annotation_layer.update import UpdateAnnotationLayerCommand
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
def mock_layer():
    layer = MagicMock()
    layer.id = 1
    layer.name = "Test Layer"
    layer.descr = "A test layer"
    layer.created_on = None
    layer.changed_on = None
    return layer


# ---------------------------------------------------------------------------
# GET single — result.id parity (upstream show_columns = [id, name, descr])
# ---------------------------------------------------------------------------


async def test_get_single_includes_result_id(mock_dao, mock_layer):
    """The detail ``result`` carries ``id`` (upstream show_columns has it),
    not only the FAB envelope top-level ``id``."""
    from superset.controllers.annotation_layer import AnnotationLayerController

    mock_dao.find_by_id = AsyncMock(return_value=mock_layer)
    handler = AnnotationLayerController.get_single
    fn = handler.fn if hasattr(handler, "fn") else handler

    resp = await fn(AnnotationLayerController(owner=MagicMock()), pk=1, dao=mock_dao)

    assert resp["id"] == 1
    assert resp["result"]["id"] == 1
    assert resp["result"]["name"] == "Test Layer"


async def test_get_single_result_excludes_timestamps(mock_dao, mock_layer):
    """Upstream show_columns = ["id", "name", "descr"] — created_on and
    changed_on must NOT appear in the single-item result dict."""
    from superset.controllers.annotation_layer import AnnotationLayerController

    mock_dao.find_by_id = AsyncMock(return_value=mock_layer)
    handler = AnnotationLayerController.get_single
    fn = handler.fn if hasattr(handler, "fn") else handler

    resp = await fn(AnnotationLayerController(owner=MagicMock()), pk=1, dao=mock_dao)

    assert set(resp["result"].keys()) == {"id", "name", "descr"}
    assert "created_on" not in resp["result"]
    assert "changed_on" not in resp["result"]


async def test_update_result_excludes_timestamps(mock_dao, mock_layer):
    """Upstream PUT returns result=item (the submitted schema dict).
    created_on and changed_on must NOT appear in the PUT result dict."""
    from superset.controllers.annotation_layer import AnnotationLayerController
    from superset.schemas.annotation import AnnotationLayerPutSchema

    mock_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.update = AsyncMock(return_value=mock_layer)

    data = AnnotationLayerPutSchema(name="Updated", descr="new desc")
    handler = AnnotationLayerController.update
    fn = handler.fn if hasattr(handler, "fn") else handler

    resp = await fn(
        AnnotationLayerController(owner=MagicMock()), pk=1, data=data, dao=mock_dao
    )

    assert resp["id"] == 1
    assert "created_on" not in resp["result"]
    assert "changed_on" not in resp["result"]
    assert resp["result"]["name"] == "Updated"
    assert resp["result"]["descr"] == "new desc"


async def test_update_result_includes_layer_key(mock_dao, mock_layer):
    """Upstream PUT adds ``item["layer"] = pk`` to the result dict before returning
    (superset_old/annotation_layers/api.py:278).  The liteset controller must echo
    ``layer`` in the result so that the response contract is 1:1 with the original."""
    from superset.controllers.annotation_layer import AnnotationLayerController
    from superset.schemas.annotation import AnnotationLayerPutSchema

    mock_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.update = AsyncMock(return_value=mock_layer)

    data = AnnotationLayerPutSchema(name="Updated", descr="new desc")
    handler = AnnotationLayerController.update
    fn = handler.fn if hasattr(handler, "fn") else handler

    resp = await fn(
        AnnotationLayerController(owner=MagicMock()), pk=5, data=data, dao=mock_dao
    )

    # result must carry the layer key echoing the path param pk
    assert resp["result"]["layer"] == 5
    # submitted fields also present
    assert resp["result"]["name"] == "Updated"
    assert resp["result"]["descr"] == "new desc"


# ---------------------------------------------------------------------------
# CreateAnnotationLayerCommand
# ---------------------------------------------------------------------------


async def test_create_layer_validates_name_required(mock_dao):
    cmd = CreateAnnotationLayerCommand(dao=mock_dao, data={})
    with pytest.raises(CommandInvalidError, match="name"):
        await cmd.validate()


async def test_create_layer_validates_empty_name(mock_dao):
    cmd = CreateAnnotationLayerCommand(dao=mock_dao, data={"name": "  "})
    with pytest.raises(CommandInvalidError, match="name"):
        await cmd.validate()


async def test_create_layer_validates_uniqueness(mock_dao):
    """Name conflict is the field-keyed 422 upstream emits:
    ``AnnotationLayerInvalidError(exceptions=[AnnotationLayerNameUniqueness
    ValidationError()])`` → ``{"name": ["Name must be unique"]}``
    (superset_old/commands/annotation_layer/exceptions.py:52-58)."""
    from superset.commands.annotation_layer.exceptions import (
        AnnotationLayerInvalidError,
    )

    mock_dao.validate_update_uniqueness = AsyncMock(return_value=False)
    cmd = CreateAnnotationLayerCommand(dao=mock_dao, data={"name": "Duplicate"})
    with pytest.raises(AnnotationLayerInvalidError) as exc_info:
        await cmd.validate()
    assert exc_info.value.normalized_messages() == {"name": ["Name must be unique"]}


async def test_create_layer_validates_success(mock_dao):
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    cmd = CreateAnnotationLayerCommand(dao=mock_dao, data={"name": "New Layer"})
    await cmd.validate()  # should not raise


async def test_create_layer_run(mock_dao, mock_layer):
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.create = AsyncMock(return_value=mock_layer)
    cmd = CreateAnnotationLayerCommand(
        dao=mock_dao, data={"name": "New Layer", "descr": "desc"}
    )
    result = await cmd.execute()
    assert result.id == 1
    assert result.name == "Test Layer"
    mock_dao.create.assert_awaited_once()
    mock_dao.session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# UpdateAnnotationLayerCommand
# ---------------------------------------------------------------------------


async def test_update_layer_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateAnnotationLayerCommand(dao=mock_dao, pk=999, data={"name": "Updated"})
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_layer_uniqueness_conflict(mock_dao, mock_layer):
    from superset.commands.annotation_layer.exceptions import (
        AnnotationLayerInvalidError,
    )

    mock_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=False)
    cmd = UpdateAnnotationLayerCommand(dao=mock_dao, pk=1, data={"name": "Duplicate"})
    with pytest.raises(AnnotationLayerInvalidError) as exc_info:
        await cmd.validate()
    assert exc_info.value.normalized_messages() == {"name": ["Name must be unique"]}


async def test_update_layer_success(mock_dao, mock_layer):
    mock_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.update = AsyncMock(return_value=mock_layer)
    cmd = UpdateAnnotationLayerCommand(dao=mock_dao, pk=1, data={"name": "Updated"})
    result = await cmd.execute()
    assert result.id == 1
    mock_dao.update.assert_awaited_once_with(mock_layer, {"name": "Updated"})


async def test_update_layer_no_name_skips_uniqueness(mock_dao, mock_layer):
    """When name is not in data, uniqueness check should be skipped."""
    mock_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.update = AsyncMock(return_value=mock_layer)
    cmd = UpdateAnnotationLayerCommand(dao=mock_dao, pk=1, data={"descr": "new desc"})
    result = await cmd.execute()
    assert result.id == 1
    mock_dao.validate_update_uniqueness.assert_not_awaited()


# ---------------------------------------------------------------------------
# DeleteAnnotationLayerCommand
# ---------------------------------------------------------------------------


async def test_delete_layer_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteAnnotationLayerCommand(dao=mock_dao, pk=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_layer_has_annotations(mock_dao, mock_layer):
    mock_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.has_annotations = AsyncMock(return_value=True)
    cmd = DeleteAnnotationLayerCommand(dao=mock_dao, pk=1)
    with pytest.raises(CommandInvalidError, match="contains annotations"):
        await cmd.validate()


async def test_delete_layer_success(mock_dao, mock_layer):
    mock_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.has_annotations = AsyncMock(return_value=False)
    mock_dao.delete = AsyncMock()
    cmd = DeleteAnnotationLayerCommand(dao=mock_dao, pk=1)
    await cmd.execute()
    mock_dao.delete.assert_awaited_once_with([mock_layer])
    mock_dao.session.flush.assert_awaited()


# ---------------------------------------------------------------------------
# BulkDeleteAnnotationLayerCommand
# ---------------------------------------------------------------------------


async def test_bulk_delete_layers_empty_ids(mock_dao):
    cmd = BulkDeleteAnnotationLayerCommand(dao=mock_dao, ids=[])
    with pytest.raises(CommandInvalidError, match="No annotation layer IDs"):
        await cmd.validate()


async def test_bulk_delete_layers_missing_ids(mock_dao, mock_layer):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_layer])
    cmd = BulkDeleteAnnotationLayerCommand(dao=mock_dao, ids=[1, 2])
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_bulk_delete_layers_has_children(mock_dao, mock_layer):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_layer])
    mock_dao.has_annotations = AsyncMock(return_value=True)
    cmd = BulkDeleteAnnotationLayerCommand(dao=mock_dao, ids=[1])
    with pytest.raises(CommandInvalidError, match="contain annotations"):
        await cmd.validate()


async def test_bulk_delete_layers_success(mock_dao, mock_layer):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_layer])
    mock_dao.has_annotations = AsyncMock(return_value=False)
    mock_dao.delete = AsyncMock()
    cmd = BulkDeleteAnnotationLayerCommand(dao=mock_dao, ids=[1])
    await cmd.execute()
    mock_dao.delete.assert_awaited_once_with([mock_layer])
    mock_dao.session.flush.assert_awaited()
