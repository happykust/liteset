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
"""Unit tests for annotation commands and AnnotationController."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.commands.annotation_layer.annotation.create import (
    CreateAnnotationCommand,
)
from superset.commands.annotation_layer.annotation.delete import (
    BulkDeleteAnnotationCommand,
    DeleteAnnotationCommand,
)
from superset.commands.annotation_layer.annotation.update import (
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
    call_args = mock_dao.create.call_args[0][0]
    assert call_args["layer_id"] == 1


async def test_update_annotation_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateAnnotationCommand(dao=mock_dao, pk=999, data={"short_descr": "updated"})
    with pytest.raises(ObjectNotFoundError, match="Annotation"):
        await cmd.validate()


async def test_update_annotation_success(mock_dao, mock_annotation):
    mock_dao.find_by_id = AsyncMock(return_value=mock_annotation)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.update = AsyncMock(return_value=mock_annotation)
    cmd = UpdateAnnotationCommand(dao=mock_dao, pk=10, data={"short_descr": "updated"})
    result = await cmd.execute()
    assert result.id == 10
    mock_dao.update.assert_awaited_once_with(
        mock_annotation, {"short_descr": "updated"}
    )
    mock_dao.session.flush.assert_awaited()


async def test_update_annotation_short_descr_absent_uses_empty_string_for_uniqueness(
    mock_dao, mock_annotation
):
    """When short_descr is absent from the PUT payload the uniqueness check must
    use '' (empty string), NOT the existing annotation's short_descr.

    ``self._properties.get('short_descr', '')`` always defaults to empty
    string when the field is absent.

    Regression guard: the old liteset code resolved the existing annotation's
    short_descr ("Test annotation"), which could raise a false 422 when that
    value happened to match another row in the same layer (duplicate legacy
    data). The original would accept the update because it checks '' which
    can never conflict.
    """
    mock_dao.find_by_id = AsyncMock(return_value=mock_annotation)

    async def _uniqueness(layer_id: int, short_descr: str, annotation_id: int) -> bool:
        return short_descr == ""

    mock_dao.validate_update_uniqueness = _uniqueness
    mock_dao.update = AsyncMock(return_value=mock_annotation)

    cmd = UpdateAnnotationCommand(
        dao=mock_dao, pk=10, data={"layer_id": 1, "long_descr": "new text"}
    )
    result = await cmd.execute()
    assert result.id == 10


async def test_update_annotation_uniqueness_raises_on_payload_conflict(
    mock_dao, mock_annotation
):
    """When short_descr IS in the payload and it conflicts, validate must raise 422."""
    from superset.commands.annotation_layer.annotation.exceptions import (
        AnnotationUniquenessValidationError,
    )

    mock_dao.find_by_id = AsyncMock(return_value=mock_annotation)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=False)

    cmd = UpdateAnnotationCommand(
        dao=mock_dao,
        pk=10,
        data={"layer_id": 1, "short_descr": "conflict"},
    )
    with pytest.raises(AnnotationUniquenessValidationError):
        await cmd.validate()


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


async def test_put_annotation_partial_update_only_submitted_fields_in_result(
    mock_dao, mock_layer_dao, mock_layer, mock_annotation
):
    """PUT with only short_descr must return result with only short_descr + layer.

    Original: ``item = edit_model_schema.load(request.json)`` contains only the
    keys present in the request body; ``item["layer"] = pk`` is appended; the
    response echoes ``item`` verbatim.  Liteset must NOT include long_descr,
    start_dttm, end_dttm, json_metadata when the client did not submit them.
    """
    from superset.controllers.annotation import AnnotationController
    from superset.schemas.annotation import AnnotationPutSchema

    mock_layer_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.find_by_id = AsyncMock(return_value=mock_annotation)
    mock_dao.update = AsyncMock(return_value=mock_annotation)
    mock_dao.session.flush = AsyncMock()

    data = AnnotationPutSchema(short_descr="only this field")

    handler = AnnotationController.update
    fn = handler.fn if hasattr(handler, "fn") else handler

    with patch("superset.controllers.annotation.event_logger") as mock_event_logger:
        mock_event_logger.alog_with_context = AsyncMock()
        resp = await fn(
            AnnotationController(owner=MagicMock()),
            pk=5,
            annotation_id=10,
            data=data,
            ann_dao=mock_dao,
            layer_dao=mock_layer_dao,
        )

    assert resp["id"] == 10
    result = resp["result"]
    assert result["short_descr"] == "only this field"
    assert result["layer"] == 5
    assert "long_descr" not in result
    assert "start_dttm" not in result
    assert "end_dttm" not in result
    assert "json_metadata" not in result


async def test_put_annotation_multi_field_update_only_submitted_in_result(
    mock_dao, mock_layer_dao, mock_layer, mock_annotation
):
    from superset.controllers.annotation import AnnotationController
    from superset.schemas.annotation import AnnotationPutSchema

    mock_layer_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.find_by_id = AsyncMock(return_value=mock_annotation)
    mock_dao.update = AsyncMock(return_value=mock_annotation)
    mock_dao.session.flush = AsyncMock()

    data = AnnotationPutSchema(
        short_descr="updated descr", json_metadata='{"key": "val"}'
    )

    handler = AnnotationController.update
    fn = handler.fn if hasattr(handler, "fn") else handler

    with patch("superset.controllers.annotation.event_logger") as mock_event_logger:
        mock_event_logger.alog_with_context = AsyncMock()
        resp = await fn(
            AnnotationController(owner=MagicMock()),
            pk=3,
            annotation_id=10,
            data=data,
            ann_dao=mock_dao,
            layer_dao=mock_layer_dao,
        )

    result = resp["result"]
    assert set(result.keys()) == {"short_descr", "json_metadata", "layer"}
    assert result["short_descr"] == "updated descr"
    assert result["json_metadata"] == '{"key": "val"}'
    assert result["layer"] == 3


async def test_put_annotation_datetime_fields_serialized_as_iso(
    mock_dao, mock_layer_dao, mock_layer, mock_annotation
):
    from superset.controllers.annotation import AnnotationController
    from superset.schemas.annotation import AnnotationPutSchema

    mock_layer_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.find_by_id = AsyncMock(return_value=mock_annotation)
    mock_dao.update = AsyncMock(return_value=mock_annotation)
    mock_dao.session.flush = AsyncMock()

    dt_start = datetime(2024, 1, 15, 10, 30, 0)
    dt_end = datetime(2024, 1, 16, 12, 0, 0)
    data = AnnotationPutSchema(start_dttm=dt_start, end_dttm=dt_end)

    handler = AnnotationController.update
    fn = handler.fn if hasattr(handler, "fn") else handler

    with patch("superset.controllers.annotation.event_logger") as mock_event_logger:
        mock_event_logger.alog_with_context = AsyncMock()
        resp = await fn(
            AnnotationController(owner=MagicMock()),
            pk=7,
            annotation_id=10,
            data=data,
            ann_dao=mock_dao,
            layer_dao=mock_layer_dao,
        )

    result = resp["result"]
    assert set(result.keys()) == {"start_dttm", "end_dttm", "layer"}
    assert result["start_dttm"] == dt_start.isoformat()
    assert result["end_dttm"] == dt_end.isoformat()
    assert result["layer"] == 7


async def test_get_list_default_page_size_is_20(mock_dao, mock_layer_dao, mock_layer):
    """FAB AnnotationRestApi.page_size = 20 (flask_appbuilder/api/__init__.py:1014);
    Liteset must pass default_page_size=20 to build_rison_query_params since
    extract_pagination defaults to 25.
    """
    from superset.controllers.annotation import AnnotationController

    mock_layer_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.find_all = AsyncMock(return_value=[])
    mock_dao.count = AsyncMock(return_value=0)

    handler = AnnotationController.get_list
    fn = handler.fn if hasattr(handler, "fn") else handler

    with (
        patch("superset.controllers.annotation.build_rison_query_params") as mock_brqp,
        patch("superset.controllers.annotation.event_logger") as mock_event_logger,
        patch("superset.controllers.annotation.serialize_list_response") as mock_slr,
    ):
        mock_brqp.return_value = ([], None, 0, 20)
        mock_event_logger.alog_with_context = AsyncMock()
        mock_slr.return_value = {"count": 0, "result": []}

        await fn(
            AnnotationController(owner=MagicMock()),
            pk=1,
            ann_dao=mock_dao,
            layer_dao=mock_layer_dao,
            rison_params=None,
        )

    assert mock_brqp.called
    _, kwargs = mock_brqp.call_args
    assert kwargs.get("default_page_size") == 20, (
        "Expected default_page_size=20 (FAB default), "
        f"got {kwargs.get('default_page_size')}"
    )


async def test_get_list_explicit_page_size_honored(
    mock_dao, mock_layer_dao, mock_layer
):
    from superset.controllers.annotation import AnnotationController

    mock_layer_dao.find_by_id = AsyncMock(return_value=mock_layer)
    mock_dao.find_all = AsyncMock(return_value=[])
    mock_dao.count = AsyncMock(return_value=0)

    handler = AnnotationController.get_list
    fn = handler.fn if hasattr(handler, "fn") else handler

    with (
        patch("superset.controllers.annotation.build_rison_query_params") as mock_brqp,
        patch("superset.controllers.annotation.event_logger") as mock_event_logger,
        patch("superset.controllers.annotation.serialize_list_response") as mock_slr,
    ):
        mock_brqp.return_value = ([], None, 0, 50)
        mock_event_logger.alog_with_context = AsyncMock()
        mock_slr.return_value = {"count": 0, "result": []}

        await fn(
            AnnotationController(owner=MagicMock()),
            pk=1,
            ann_dao=mock_dao,
            layer_dao=mock_layer_dao,
            rison_params={"page": 0, "page_size": 50},
        )

    mock_dao.find_all.assert_awaited_once()
    call_kwargs = mock_dao.find_all.call_args[1]
    assert call_kwargs["page_size"] == 50
