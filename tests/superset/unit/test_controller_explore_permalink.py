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
"""Tests for ExplorePermalinkController."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import msgspec
import pytest

from superset.controllers.explore_permalink import (
    ExplorePermalinkController,
    ExplorePermalinkCreateSchema,
)

# ---------------------------------------------------------------------------
# Controller metadata
# ---------------------------------------------------------------------------


def test_controller_path():
    assert ExplorePermalinkController.path == "/api/v1/explore/permalink"


def test_controller_tags():
    assert ExplorePermalinkController.tags == ["Explore Permalink"]


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_create_body_defaults():
    # form_data is required; url_params defaults to None.
    body = ExplorePermalinkCreateSchema(form_data={})
    assert body.form_data == {}
    assert body.url_params is None


def test_create_body_with_values():
    # The wire payload uses camelCase keys (struct rename="camel").
    body = msgspec.convert(
        {
            "formData": {"viz_type": "table"},
            "urlParams": [["foo", "bar"]],
        },
        ExplorePermalinkCreateSchema,
    )
    assert body.form_data == {"viz_type": "table"}
    assert body.url_params == [["foo", "bar"]]


# ---------------------------------------------------------------------------
# Handler logic tests (call underlying fn directly)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_kv_dao():
    return AsyncMock()


@pytest.fixture
def mock_user():
    user = MagicMock()
    type(user).id = PropertyMock(return_value=1)
    return user


async def test_create_permalink(mock_kv_dao, mock_user):
    # form_data must carry a parseable ``datasource`` ('<id>__<type>');
    # ``slice_id`` (optional) becomes the chart_id used by check_access.
    data = ExplorePermalinkCreateSchema(
        form_data={"datasource": "1__table", "slice_id": 1, "viz_type": "table"}
    )
    entry = MagicMock()
    entry.id = 42
    fake_dao = AsyncMock()
    fake_dao.create_entry.return_value = entry
    session = AsyncMock()
    create_fn = ExplorePermalinkController.create_permalink.fn

    with (
        patch(
            "superset.controllers.explore_permalink.check_chart_access",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "superset.controllers.explore_permalink.AsyncKeyValueDAO",
            return_value=fake_dao,
        ),
        patch(
            "superset.controllers.explore_permalink.get_permalink_salt",
            new=AsyncMock(return_value="salt"),
        ),
        patch(
            "superset.controllers.explore_permalink.encode_permalink_key",
            return_value="abc123",
        ),
        patch.object(
            ExplorePermalinkController.create_permalink.fn.__globals__["event_logger"],
            "alog_with_context",
            new=AsyncMock(),
        ),
    ):
        result = await create_fn(
            None,
            data=data,
            kv_dao=mock_kv_dao,
            chart_dao=AsyncMock(),
            dataset_dao=AsyncMock(),
            query_dao=AsyncMock(),
            current_user=mock_user,
            security_manager=MagicMock(),
            session=session,
        )
    assert result["key"] == "abc123"
    assert result["url"].startswith("/explore/p/")
    fake_dao.create_entry.assert_awaited_once()


async def test_get_permalink_found(mock_kv_dao, mock_user):
    entry = MagicMock()
    entry.value = json.dumps(
        {"chartId": 1, "datasourceId": 1, "datasourceType": "table"}
    ).encode("utf-8")
    fake_dao = AsyncMock()
    fake_dao.get_entry_by_key.return_value = entry
    get_fn = ExplorePermalinkController.get_permalink.fn

    with (
        patch(
            "superset.controllers.explore_permalink.check_chart_access",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "superset.controllers.explore_permalink.AsyncKeyValueDAO",
            return_value=fake_dao,
        ),
        patch(
            "superset.controllers.explore_permalink.get_permalink_salt",
            new=AsyncMock(return_value="salt"),
        ),
        patch(
            "superset.controllers.explore_permalink.decode_permalink_id",
            return_value=42,
        ),
    ):
        result = await get_fn(
            None,
            key="abc123",
            kv_dao=mock_kv_dao,
            chart_dao=AsyncMock(),
            dataset_dao=AsyncMock(),
            query_dao=AsyncMock(),
            current_user=mock_user,
            security_manager=MagicMock(),
            session=AsyncMock(),
        )
    assert result["chartId"] == 1


async def test_get_permalink_not_found(mock_kv_dao, mock_user):
    from superset.exceptions import ObjectNotFoundError

    fake_dao = AsyncMock()
    fake_dao.get_entry_by_key.return_value = None
    get_fn = ExplorePermalinkController.get_permalink.fn

    with (
        patch(
            "superset.controllers.explore_permalink.AsyncKeyValueDAO",
            return_value=fake_dao,
        ),
        patch(
            "superset.controllers.explore_permalink.get_permalink_salt",
            new=AsyncMock(return_value="salt"),
        ),
        patch(
            "superset.controllers.explore_permalink.decode_permalink_id",
            return_value=999,
        ),
    ):
        with pytest.raises(ObjectNotFoundError):
            await get_fn(
                None,
                key="missing",
                kv_dao=mock_kv_dao,
                chart_dao=AsyncMock(),
                dataset_dao=AsyncMock(),
                query_dao=AsyncMock(),
                current_user=mock_user,
                security_manager=MagicMock(),
                session=AsyncMock(),
            )
