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
"""Unit tests for RLS controller and commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.rls import (
    BulkDeleteRLSCommand,
    CreateRLSCommand,
    DeleteRLSCommand,
    UpdateRLSCommand,
)
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.controllers.rls import RLSController


@pytest.fixture
def mock_dao() -> AsyncMock:
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    return dao


# ---------------------------------------------------------------------------
# Controller metadata
# ---------------------------------------------------------------------------


def test_rls_controller_path():
    assert RLSController.path == "/api/v1/rowlevelsecurity"


def test_rls_controller_tags():
    assert RLSController.tags == ["Row Level Security"]


# ---------------------------------------------------------------------------
# CreateRLSCommand
# ---------------------------------------------------------------------------


async def test_create_rls_validates_name(mock_dao: AsyncMock):
    cmd = CreateRLSCommand(dao=mock_dao, data={"clause": "1=1"})
    with pytest.raises(CommandInvalidError, match="name is required"):
        await cmd.validate()


async def test_create_rls_validates_empty_name(mock_dao: AsyncMock):
    cmd = CreateRLSCommand(dao=mock_dao, data={"name": "  ", "clause": "1=1"})
    with pytest.raises(CommandInvalidError, match="name is required"):
        await cmd.validate()


async def test_create_rls_validates_clause(mock_dao: AsyncMock):
    cmd = CreateRLSCommand(dao=mock_dao, data={"name": "Test"})
    with pytest.raises(CommandInvalidError, match="clause is required"):
        await cmd.validate()


async def test_create_rls_validates_empty_clause(mock_dao: AsyncMock):
    cmd = CreateRLSCommand(dao=mock_dao, data={"name": "Test", "clause": ""})
    with pytest.raises(CommandInvalidError, match="clause is required"):
        await cmd.validate()


async def test_create_rls_validates_filter_type(mock_dao: AsyncMock):
    cmd = CreateRLSCommand(
        dao=mock_dao, data={"name": "Test", "clause": "1=1", "filter_type": "Invalid"}
    )
    with pytest.raises(CommandInvalidError, match="filter_type"):
        await cmd.validate()


async def test_create_rls_valid(mock_dao: AsyncMock):
    """Valid data passes validation."""
    cmd = CreateRLSCommand(
        dao=mock_dao,
        data={"name": "Test", "clause": "client_id = 1", "filter_type": "Regular"},
    )
    await cmd.validate()  # Should not raise


async def test_create_rls_run(mock_dao: AsyncMock):
    """run() delegates to dao.create."""
    mock_item = MagicMock(id=1)
    mock_dao.create = AsyncMock(return_value=mock_item)
    data = {"name": "Test", "clause": "1=1"}
    cmd = CreateRLSCommand(dao=mock_dao, data=data)
    result = await cmd.run()
    mock_dao.create.assert_called_once_with(data)
    assert result.id == 1


# ---------------------------------------------------------------------------
# UpdateRLSCommand
# ---------------------------------------------------------------------------


async def test_update_rls_not_found(mock_dao: AsyncMock):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateRLSCommand(dao=mock_dao, pk=999, data={"name": "New"})
    with pytest.raises(ObjectNotFoundError, match="999"):
        await cmd.validate()


async def test_update_rls_invalid_filter_type(mock_dao: AsyncMock):
    mock_dao.find_by_id = AsyncMock(return_value=MagicMock(id=1))
    cmd = UpdateRLSCommand(
        dao=mock_dao, pk=1, data={"filter_type": "BadType"}
    )
    with pytest.raises(CommandInvalidError, match="filter_type"):
        await cmd.validate()


async def test_update_rls_success(mock_dao: AsyncMock):
    existing = MagicMock(id=1)
    mock_dao.find_by_id = AsyncMock(return_value=existing)
    mock_dao.update = AsyncMock(return_value=existing)
    cmd = UpdateRLSCommand(dao=mock_dao, pk=1, data={"name": "Updated"})
    await cmd.validate()
    result = await cmd.run()
    mock_dao.update.assert_called_once_with(existing, {"name": "Updated"})
    assert result.id == 1


# ---------------------------------------------------------------------------
# DeleteRLSCommand
# ---------------------------------------------------------------------------


async def test_delete_rls_not_found(mock_dao: AsyncMock):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteRLSCommand(dao=mock_dao, pk=999)
    with pytest.raises(ObjectNotFoundError, match="999"):
        await cmd.validate()


async def test_delete_rls_success(mock_dao: AsyncMock):
    existing = MagicMock(id=1)
    mock_dao.find_by_id = AsyncMock(return_value=existing)
    mock_dao.delete = AsyncMock()
    cmd = DeleteRLSCommand(dao=mock_dao, pk=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_called_once_with([existing])


# ---------------------------------------------------------------------------
# BulkDeleteRLSCommand
# ---------------------------------------------------------------------------


async def test_bulk_delete_rls_empty_ids(mock_dao: AsyncMock):
    cmd = BulkDeleteRLSCommand(dao=mock_dao, ids=[])
    with pytest.raises(CommandInvalidError, match="No IDs"):
        await cmd.validate()


async def test_bulk_delete_rls_success(mock_dao: AsyncMock):
    mock_dao.bulk_delete = AsyncMock(return_value=3)
    cmd = BulkDeleteRLSCommand(dao=mock_dao, ids=[1, 2, 3])
    await cmd.validate()
    result = await cmd.run()
    assert result == 3
    mock_dao.bulk_delete.assert_called_once_with([1, 2, 3])


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_rls_post_schema_valid():
    import msgspec

    from superset.schemas.rls import RLSPostSchema

    body = msgspec.convert(
        {"name": "Test", "filter_type": "Regular", "clause": "1=1"},
        RLSPostSchema,
    )
    assert body.name == "Test"
    assert body.clause == "1=1"
    assert body.tables == []
    assert body.roles == []


def test_rls_post_schema_name_required():
    import msgspec

    from superset.schemas.rls import RLSPostSchema

    with pytest.raises(msgspec.ValidationError):
        msgspec.convert(
            {"name": "", "filter_type": "Regular", "clause": "1=1"},
            RLSPostSchema,
        )


def test_rls_post_schema_clause_required():
    import msgspec

    from superset.schemas.rls import RLSPostSchema

    with pytest.raises(msgspec.ValidationError):
        msgspec.convert(
            {"name": "Test", "filter_type": "Regular", "clause": ""},
            RLSPostSchema,
        )


def test_rls_put_schema_all_unset():
    import msgspec

    from superset.schemas.rls import RLSPutSchema

    body = msgspec.convert({}, RLSPutSchema)
    assert body.name is msgspec.UNSET
    assert body.clause is msgspec.UNSET
    assert body.filter_type is msgspec.UNSET


def test_rls_put_schema_partial():
    import msgspec

    from superset.schemas.rls import RLSPutSchema

    body = msgspec.convert({"name": "Updated"}, RLSPutSchema)
    assert body.name == "Updated"
    assert body.clause is msgspec.UNSET
