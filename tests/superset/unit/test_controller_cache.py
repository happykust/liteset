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

from unittest.mock import AsyncMock, patch

import msgspec
import pytest

from superset.controllers.cache import CacheController, CacheInvalidateSchema


def test_cache_invalidate_body_accepts_uids():
    """CacheInvalidateSchema accepts a list of datasource UIDs."""
    body = msgspec.convert(
        {"datasource_uids": ["table__1", "table__2"]},
        CacheInvalidateSchema,
    )
    assert body.datasource_uids == ["table__1", "table__2"]


def test_cache_invalidate_body_empty_list():
    """CacheInvalidateSchema accepts an empty list."""
    body = msgspec.convert({"datasource_uids": []}, CacheInvalidateSchema)
    assert body.datasource_uids == []


def test_cache_invalidate_body_missing_field():
    """Both fields are optional (neither ``datasource_uids`` nor ``datasources``
    is ``required``); an empty body is valid and the handler falls through to a
    201 no-op."""
    body = msgspec.convert({}, CacheInvalidateSchema)
    assert body.datasource_uids == []
    assert body.datasources == []


def test_cache_invalidate_body_snake_case_wire_contract():
    """Regression: a previous ``rename="camel"`` silently dropped a correctly-formed
    ``datasource_uids`` body → 201 invalidating nothing."""
    body = msgspec.convert(
        {
            "datasource_uids": ["table__1"],
            "datasources": [
                {
                    "database_name": "examples",
                    "datasource_name": "energy_usage",
                    "datasource_type": "table",
                }
            ],
        },
        CacheInvalidateSchema,
    )
    assert body.datasource_uids == ["table__1"]
    assert body.datasources[0].database_name == "examples"
    assert body.datasources[0].datasource_name == "energy_usage"


def test_cache_invalidate_body_wrong_type():
    """CacheInvalidateSchema rejects non-list values."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"datasource_uids": "not-a-list"}, CacheInvalidateSchema)


def test_cache_controller_path():
    assert CacheController.path == "/api/v1/cachekey"


def test_cache_controller_tags():
    assert CacheController.tags == ["Cache"]


async def test_do_invalidate_logs_object_ref():
    """_do_invalidate must pass object_ref="CacheRestApi.invalidate" to
    alog_with_context. The ``@event_logger.log_this_with_context(log_to_statsd=False)``
    decorator computes
    ``object_ref_str = None or f.__qualname__ = "CacheRestApi.invalidate"``
    and passes it through to log_with_context.

    Without this, the logs.json field is absent for every cache-invalidation
    request — an admin-visible regression.
    """
    controller = object.__new__(CacheController)

    mock_dao = AsyncMock()
    from litestar.response import Response as LitestarResponse

    empty_resp = LitestarResponse(
        content=b"{}",
        status_code=201,
        media_type="application/json",
    )

    with patch.object(
        controller, "_invalidate_body", AsyncMock(return_value=empty_resp)
    ):
        with patch("superset.controllers.cache.event_logger") as mock_event_logger:
            mock_event_logger.alog_with_context = AsyncMock()

            data = msgspec.convert({}, CacheInvalidateSchema)
            await controller._do_invalidate(data, mock_dao)

    mock_event_logger.alog_with_context.assert_called_once()
    call_kwargs = mock_event_logger.alog_with_context.call_args
    assert call_kwargs.kwargs.get("object_ref") == "CacheRestApi.invalidate"
    assert call_kwargs.kwargs.get("log_to_statsd") is False
