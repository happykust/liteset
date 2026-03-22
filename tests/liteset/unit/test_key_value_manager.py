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
"""Tests for AsyncKeyValueManager."""

from __future__ import annotations

import pickle
from unittest.mock import AsyncMock, MagicMock

import pytest

from liteset.key_value.manager import (
    AsyncKeyValueManager,
    JsonCodec,
    PickleCodec,
)


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    return dao


@pytest.fixture
def manager(mock_dao):
    return AsyncKeyValueManager(dao=mock_dao)


async def test_get_returns_none_when_not_found(manager, mock_dao):
    mock_dao.get_entry.return_value = None
    result = await manager.get("test_resource", 1)
    assert result is None


async def test_get_decodes_json(manager, mock_dao):
    entry = MagicMock()
    entry.value = b'{"key": "value"}'
    mock_dao.get_entry.return_value = entry
    result = await manager.get("test_resource", 1)
    assert result == {"key": "value"}


async def test_set_creates_new_entry(manager, mock_dao):
    entry = MagicMock()
    entry.id = 42
    mock_dao.create_entry.return_value = entry
    result = await manager.set("test_resource", {"data": "test"})
    assert result == 42
    mock_dao.create_entry.assert_called_once()


async def test_set_upserts_with_key(manager, mock_dao):
    entry = MagicMock()
    entry.id = 10
    mock_dao.upsert_entry.return_value = entry
    result = await manager.set("test_resource", {"data": "test"}, key=10)
    assert result == 10
    mock_dao.upsert_entry.assert_called_once()


async def test_delete_delegates_to_dao(manager, mock_dao):
    mock_dao.delete_entry.return_value = True
    result = await manager.delete("test_resource", 1)
    assert result is True


async def test_json_codec_roundtrip():
    codec = JsonCodec()
    data = {"key": "value", "num": 42}
    encoded = codec.encode(data)
    decoded = codec.decode(encoded)
    assert decoded == data


async def test_pickle_codec_roundtrip():
    codec = PickleCodec()
    data = {"key": "value", "num": 42}
    encoded = codec.encode(data)
    decoded = codec.decode(encoded)
    assert decoded == data


async def test_get_with_pickle_codec(manager, mock_dao):
    entry = MagicMock()
    entry.value = pickle.dumps({"test": True})
    mock_dao.get_entry.return_value = entry
    result = await manager.get("res", 1, codec=PickleCodec())
    assert result == {"test": True}
