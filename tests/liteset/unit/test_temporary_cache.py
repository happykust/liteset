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
"""Tests for TemporaryCacheController."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from liteset.controllers.temporary_cache import TemporaryCacheBody, TemporaryCacheController
from liteset.exceptions import ObjectNotFoundError


class _CacheController(TemporaryCacheController):
    path = "/api/v1/test_cache"
    resource = "test_cache"


@pytest.fixture
def controller():
    # Bypass Litestar Controller.__init__ which requires a router owner
    return object.__new__(_CacheController)


@pytest.fixture
def mock_kv_dao():
    return AsyncMock()


@pytest.fixture
def mock_user():
    user = MagicMock()
    type(user).id = PropertyMock(return_value=1)
    return user


# Access the underlying function via .fn to bypass Litestar's
# HTTPRouteHandler.__call__ which expects a resolved connection object.

async def test_get_value_found(controller, mock_kv_dao, mock_user):
    envelope = json.dumps({"owner": 1, "value": "test_data"})
    mock_kv_dao.get_value.return_value = envelope
    fn = _CacheController.get_value.fn
    result = await fn(controller, key="abc", kv_dao=mock_kv_dao, current_user=mock_user)
    assert result["value"] == "test_data"


async def test_get_value_not_found(controller, mock_kv_dao, mock_user):
    mock_kv_dao.get_value.return_value = None
    fn = _CacheController.get_value.fn
    with pytest.raises(ObjectNotFoundError):
        await fn(controller, key="missing", kv_dao=mock_kv_dao, current_user=mock_user)


async def test_create_value(controller, mock_kv_dao, mock_user):
    data = TemporaryCacheBody(value="new_data")
    fn = _CacheController.create_value.fn
    result = await fn(controller, data=data, kv_dao=mock_kv_dao, current_user=mock_user)
    assert "key" in result
    mock_kv_dao.set_value.assert_called_once()


async def test_update_value_found(controller, mock_kv_dao, mock_user):
    mock_kv_dao.get_value.return_value = "existing"
    data = TemporaryCacheBody(value="updated")
    fn = _CacheController.update_value.fn
    result = await fn(controller, key="abc", data=data, kv_dao=mock_kv_dao, current_user=mock_user)
    assert result["key"] == "abc"


async def test_update_value_not_found(controller, mock_kv_dao, mock_user):
    mock_kv_dao.get_value.return_value = None
    data = TemporaryCacheBody(value="updated")
    fn = _CacheController.update_value.fn
    with pytest.raises(ObjectNotFoundError):
        await fn(controller, key="missing", data=data, kv_dao=mock_kv_dao, current_user=mock_user)


async def test_delete_value(controller, mock_kv_dao, mock_user):
    mock_kv_dao.delete_value.return_value = True
    fn = _CacheController.delete_value.fn
    result = await fn(controller, key="abc", kv_dao=mock_kv_dao, current_user=mock_user)
    assert result["message"] == "OK"


async def test_delete_not_found(controller, mock_kv_dao, mock_user):
    mock_kv_dao.delete_value.return_value = False
    fn = _CacheController.delete_value.fn
    with pytest.raises(ObjectNotFoundError):
        await fn(controller, key="missing", kv_dao=mock_kv_dao, current_user=mock_user)
