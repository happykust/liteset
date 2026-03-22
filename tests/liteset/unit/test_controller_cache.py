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
"""Unit tests for the Cache controller."""

from __future__ import annotations

import msgspec
import pytest

from liteset.controllers.cache import CacheController, CacheInvalidateBody


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_cache_invalidate_body_accepts_uids():
    """CacheInvalidateBody accepts a list of datasource UIDs."""
    body = msgspec.convert(
        {"datasource_uids": ["table__1", "table__2"]},
        CacheInvalidateBody,
    )
    assert body.datasource_uids == ["table__1", "table__2"]


def test_cache_invalidate_body_empty_list():
    """CacheInvalidateBody accepts an empty list."""
    body = msgspec.convert({"datasource_uids": []}, CacheInvalidateBody)
    assert body.datasource_uids == []


def test_cache_invalidate_body_missing_field():
    """CacheInvalidateBody requires datasource_uids."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({}, CacheInvalidateBody)


def test_cache_invalidate_body_wrong_type():
    """CacheInvalidateBody rejects non-list values."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"datasource_uids": "not-a-list"}, CacheInvalidateBody)


# ---------------------------------------------------------------------------
# Controller metadata
# ---------------------------------------------------------------------------


def test_cache_controller_path():
    """CacheController is mounted at the correct path."""
    assert CacheController.path == "/api/v1/cachekey"


def test_cache_controller_tags():
    """CacheController has expected tags."""
    assert CacheController.tags == ["Cache"]
