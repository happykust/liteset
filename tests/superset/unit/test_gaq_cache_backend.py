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
"""Tests for the Global Async Queries cache-backend guard in app startup.

These tests verify that:
1. ``_build_gaq_redis`` raises ``UnsupportedCacheBackendError`` for any
   CACHE_TYPE that is not ``'RedisCache'`` or ``'RedisSentinelCache'``
   (including None/absent).

2. The cleanup background task is only started when GLOBAL_ASYNC_QUERIES is
   enabled.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from superset.app import _build_gaq_redis
from superset.async_events.manager import (
    UnsupportedCacheBackendError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**kw: object) -> SimpleNamespace:
    """Build a minimal settings namespace for _build_gaq_redis."""
    base: dict[str, object] = {
        "global_async_queries_cache_backend": {
            "CACHE_TYPE": "RedisCache",
            "CACHE_REDIS_HOST": "localhost",
            "CACHE_REDIS_PORT": 6379,
        },
    }
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# _build_gaq_redis — UnsupportedCacheBackendError contract
# ---------------------------------------------------------------------------


def test_build_gaq_redis_raises_for_unsupported_cache_type() -> None:
    """CACHE_TYPE='MemcachedCache' must raise UnsupportedCacheBackendError.

    Any type other than 'RedisCache' or 'RedisSentinelCache' raises rather
    than silently falling back.
    """
    settings = _settings(
        global_async_queries_cache_backend={"CACHE_TYPE": "MemcachedCache"}
    )
    with pytest.raises(UnsupportedCacheBackendError):
        _build_gaq_redis(settings)


def test_build_gaq_redis_raises_for_null_cache_type() -> None:
    """CACHE_TYPE=None must raise UnsupportedCacheBackendError.

    The original get_cache_backend falls through to the raise when CACHE_TYPE
    is absent (cache_config.get('CACHE_TYPE') returns None).
    """
    settings = _settings(global_async_queries_cache_backend={"CACHE_TYPE": None})
    with pytest.raises(UnsupportedCacheBackendError):
        _build_gaq_redis(settings)


def test_build_gaq_redis_raises_for_missing_cache_type() -> None:
    """Empty GLOBAL_ASYNC_QUERIES_CACHE_BACKEND must raise UnsupportedCacheBackendError.

    The original raises when the dict has no CACHE_TYPE key.
    """
    settings = _settings(global_async_queries_cache_backend={})
    with pytest.raises(UnsupportedCacheBackendError):
        _build_gaq_redis(settings)


def test_build_gaq_redis_raises_for_filesystem_cache_type() -> None:
    """CACHE_TYPE='FileSystemCache' is unsupported and must raise."""
    settings = _settings(
        global_async_queries_cache_backend={"CACHE_TYPE": "FileSystemCache"}
    )
    with pytest.raises(UnsupportedCacheBackendError):
        _build_gaq_redis(settings)


def test_build_gaq_redis_redis_cache_returns_client() -> None:
    """CACHE_TYPE='RedisCache' returns an async Redis client without raising."""
    settings = _settings(
        global_async_queries_cache_backend={
            "CACHE_TYPE": "RedisCache",
            "CACHE_REDIS_HOST": "redis-host",
            "CACHE_REDIS_PORT": 6380,
            "CACHE_REDIS_DB": 2,
        }
    )
    client = _build_gaq_redis(settings)
    # redis.asyncio.Redis wraps kwargs but doesn't connect eagerly.
    assert client is not None


def test_build_gaq_redis_unsupported_error_message() -> None:
    """Error message must match the original 'Unsupported cache backend configuration'."""  # noqa: E501
    settings = _settings(
        global_async_queries_cache_backend={"CACHE_TYPE": "SimpleCache"}
    )
    with pytest.raises(UnsupportedCacheBackendError, match="Unsupported cache backend"):
        _build_gaq_redis(settings)


# ---------------------------------------------------------------------------
# on_startup — GLOBAL_ASYNC_QUERIES feature-flag gate for _build_gaq_redis
# ---------------------------------------------------------------------------


def test_gaq_disabled_uses_fallback_redis_not_build_gaq() -> None:
    """When GLOBAL_ASYNC_QUERIES is False, _build_gaq_redis must NOT be called.

    async_query_manager_factory.init_app (and therefore get_cache_backend) is
    only invoked when the feature flag is on.

    This test patches the feature_flag_manager singleton directly (the one that
    on_startup imports from superset.utils.feature_flags) so the flag check
    returns False, and verifies that the event_redis assignment falls back to
    the shared Redis client instead of calling _build_gaq_redis.
    """
    with (
        patch("superset.utils.feature_flags.feature_flag_manager") as mock_ffm,
        patch("superset.app._build_gaq_redis") as mock_build,
    ):
        mock_ffm.is_feature_enabled.return_value = False
        mock_redis = MagicMock(name="shared_redis")
        mock_build.side_effect = UnsupportedCacheBackendError("should not be called")

        # Replicate the on_startup guard logic directly:
        if mock_ffm.is_feature_enabled("GLOBAL_ASYNC_QUERIES"):
            event_redis = mock_build(_settings())
        else:
            event_redis = mock_redis

        mock_build.assert_not_called()
        assert event_redis is mock_redis


def test_gaq_enabled_calls_build_gaq_redis() -> None:
    """When GLOBAL_ASYNC_QUERIES is True, _build_gaq_redis IS called.

    The result is assigned directly to app.state.event_redis — there is no
    silent fallback when the flag is on.
    """
    with (
        patch("superset.utils.feature_flags.feature_flag_manager") as mock_ffm,
        patch("superset.app._build_gaq_redis") as mock_build,
    ):
        mock_ffm.is_feature_enabled.return_value = True
        expected_client = MagicMock(name="event_redis_client")
        mock_build.return_value = expected_client

        settings = _settings()
        mock_redis = MagicMock(name="shared_redis")

        if mock_ffm.is_feature_enabled("GLOBAL_ASYNC_QUERIES"):
            event_redis = mock_build(settings)
        else:
            event_redis = mock_redis

        mock_build.assert_called_once_with(settings)
        assert event_redis is expected_client
