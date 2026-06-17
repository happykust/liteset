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
"""Tests for the Global Async Queries JWT-secret length guard.

When ``GLOBAL_ASYNC_QUERIES`` is enabled the app must refuse to start if the
JWT secret is shorter than 32 bytes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from superset.app import _validate_global_async_queries_config
from superset.commands.chart.data.create_async_job_command import (
    AsyncQueryTokenException,
)


class _SecretStr:
    """Minimal SecretStr stand-in exposing get_secret_value()."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


def _settings(**kw):
    base = {
        "global_async_queries": False,
        "global_async_queries_jwt_secret": None,
        "secret_key": "",
        # Non-null cache backends so the cache-null guard (which runs BEFORE
        # the JWT check) passes and these tests reach the JWT-secret logic
        # they target.
        "cache_config": {"CACHE_TYPE": "RedisCache"},
        "data_cache_config": {"CACHE_TYPE": "RedisCache"},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_gaq_enabled_null_cache_raises():
    # GAQ requires non-null CACHE_CONFIG/DATA_CACHE_CONFIG; a null cache type
    # refuses to start.
    with pytest.raises(Exception, match="Cache backends"):
        _validate_global_async_queries_config(
            _settings(
                global_async_queries=True,
                global_async_queries_jwt_secret="x" * 32,
                cache_config={"CACHE_TYPE": "null"},
            )
        )


def test_gaq_disabled_short_secret_is_ignored():
    # Validation only runs when the feature flag is on (matches original gating).
    _validate_global_async_queries_config(
        _settings(global_async_queries=False, global_async_queries_jwt_secret="short")
    )  # no raise


def test_gaq_enabled_short_secret_raises():
    with pytest.raises(AsyncQueryTokenException) as exc:
        _validate_global_async_queries_config(
            _settings(
                global_async_queries=True,
                global_async_queries_jwt_secret="test-secret-change-me",  # 21 bytes
            )
        )
    assert "at least 32 bytes" in str(exc.value)


def test_gaq_enabled_exactly_32_bytes_ok():
    _validate_global_async_queries_config(
        _settings(
            global_async_queries=True,
            global_async_queries_jwt_secret="x" * 32,
        )
    )  # no raise


def test_gaq_enabled_31_bytes_raises():
    with pytest.raises(AsyncQueryTokenException):
        _validate_global_async_queries_config(
            _settings(
                global_async_queries=True,
                global_async_queries_jwt_secret="y" * 31,
            )
        )


def test_gaq_enabled_falls_back_to_long_secret_key():
    # GAQ secret unset -> _resolve_secret_key falls back to SECRET_KEY; a long
    # SECRET_KEY satisfies the guard.
    _validate_global_async_queries_config(
        _settings(
            global_async_queries=True,
            global_async_queries_jwt_secret=None,
            secret_key="k" * 40,
        )
    )  # no raise


def test_gaq_enabled_secretstr_unwrapped_then_checked():
    with pytest.raises(AsyncQueryTokenException):
        _validate_global_async_queries_config(
            _settings(
                global_async_queries=True,
                global_async_queries_jwt_secret=_SecretStr("short"),
            )
        )
