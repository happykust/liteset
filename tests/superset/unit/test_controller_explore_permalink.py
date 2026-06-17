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

import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import msgspec
import pytest

from superset.controllers.explore_permalink import (
    ExplorePermalinkController,
    ExplorePermalinkCreateSchema,
)


def test_controller_path():
    assert ExplorePermalinkController.path == "/api/v1/explore/permalink"


def test_controller_tags():
    assert ExplorePermalinkController.tags == ["Explore Permalink"]


def test_create_body_defaults():
    body = ExplorePermalinkCreateSchema(form_data={})
    assert body.form_data == {}
    assert body.url_params is msgspec.UNSET


def test_create_body_with_values():
    body = msgspec.convert(
        {
            "formData": {"viz_type": "table"},
            "urlParams": [["foo", "bar"]],
        },
        ExplorePermalinkCreateSchema,
    )
    assert body.form_data == {"viz_type": "table"}
    assert body.url_params == [("foo", "bar")]


def test_create_body_url_params_allows_null_pair_elements():
    """fields.Tuple((String(allow_none=True), String(allow_none=True))) — each
    element may be null."""
    body = msgspec.convert(
        {"formData": {}, "urlParams": [["k", None]]},
        ExplorePermalinkCreateSchema,
    )
    assert body.url_params == [("k", None)]


def test_create_body_url_params_rejects_wrong_arity():
    """Upstream fields.Tuple enforces exactly two elements;
    over-length pair rejected.
    """
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert(
            {"formData": {}, "urlParams": [["a", "b", "c"]]},
            ExplorePermalinkCreateSchema,
        )


@pytest.fixture
def mock_kv_dao():
    return AsyncMock()


@pytest.fixture
def mock_user():
    user = MagicMock()
    type(user).id = PropertyMock(return_value=1)
    return user


async def test_create_permalink(mock_kv_dao, mock_user):
    data = ExplorePermalinkCreateSchema(
        form_data={"datasource": "1__table", "slice_id": 1, "viz_type": "table"}
    )
    entry = MagicMock()
    entry.id = 42
    fake_dao = AsyncMock()
    fake_dao.create_entry.return_value = entry
    session = AsyncMock()
    create_fn = ExplorePermalinkController.create_permalink.fn
    mock_request = MagicMock()
    mock_request.base_url = "http://localhost:8088/"

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
            request=mock_request,
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
    assert "/superset/explore/p/" in result["url"]
    fake_dao.create_entry.assert_awaited_once()


async def test_get_permalink_found(mock_kv_dao, mock_user):
    entry = MagicMock()
    entry.value = json.dumps(
        {"chartId": 1, "datasourceId": 1, "datasourceType": "table"}
    ).encode("utf-8")
    # Non-expiring permalink: ``get_permalink`` treats an entry as missing (404)
    # when ``expires_on`` is in the past (mirrors ``KeyValueEntry.is_expired()``).
    # Non-expiring permalink: treats an entry as missing when expires_on is past.
    entry.expires_on = None
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


async def test_create_permalink_missing_datasource_raises_command_invalid():
    """Missing 'datasource' key: previously unhandled KeyError → 500;
    now CommandInvalidError → 422.
    """
    from superset.exceptions import CommandInvalidError

    data = ExplorePermalinkCreateSchema(form_data={"viz_type": "table"})
    create_fn = ExplorePermalinkController.create_permalink.fn
    mock_request = MagicMock()
    mock_request.base_url = "http://localhost:8088/"

    with pytest.raises(CommandInvalidError):
        await create_fn(
            None,
            request=mock_request,
            data=data,
            kv_dao=AsyncMock(),
            chart_dao=AsyncMock(),
            dataset_dao=AsyncMock(),
            query_dao=AsyncMock(),
            current_user=MagicMock(id=1),
            security_manager=MagicMock(),
            session=AsyncMock(),
        )


async def test_create_permalink_malformed_datasource_raises_command_invalid():
    """datasource without '__' separator: previously unhandled ValueError → 500;
    now CommandInvalidError → 422.
    """
    from superset.exceptions import CommandInvalidError

    data = ExplorePermalinkCreateSchema(
        form_data={"datasource": "nodash", "viz_type": "table"}
    )
    create_fn = ExplorePermalinkController.create_permalink.fn
    mock_request = MagicMock()
    mock_request.base_url = "http://localhost:8088/"

    with pytest.raises(CommandInvalidError):
        await create_fn(
            None,
            request=mock_request,
            data=data,
            kv_dao=AsyncMock(),
            chart_dao=AsyncMock(),
            dataset_dao=AsyncMock(),
            query_dao=AsyncMock(),
            current_user=MagicMock(id=1),
            security_manager=MagicMock(),
            session=AsyncMock(),
        )


async def test_create_permalink_url_params_none_omitted_from_state():
    """msgspec cannot distinguish absent url_params from explicit null — both
    produce url_params=None and 'urlParams' is omitted from the stored state.
    Behavior matches original for the common case (field absent)."""
    data = ExplorePermalinkCreateSchema(form_data={"datasource": "1__table"})
    entry = MagicMock()
    entry.id = 1
    fake_dao = AsyncMock()
    fake_dao.create_entry.return_value = entry
    session = AsyncMock()
    create_fn = ExplorePermalinkController.create_permalink.fn
    mock_request = MagicMock()
    mock_request.base_url = "http://localhost:8088/"

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
            return_value="key1",
        ),
        patch.object(
            ExplorePermalinkController.create_permalink.fn.__globals__["event_logger"],
            "alog_with_context",
            new=AsyncMock(),
        ),
    ):
        await create_fn(
            None,
            request=mock_request,
            data=data,
            kv_dao=AsyncMock(),
            chart_dao=AsyncMock(),
            dataset_dao=AsyncMock(),
            query_dao=AsyncMock(),
            current_user=MagicMock(id=1),
            security_manager=MagicMock(),
            session=session,
        )

    call_kwargs = fake_dao.create_entry.call_args.kwargs
    stored = json.loads(call_kwargs["value"].decode("utf-8"))
    assert "urlParams" not in stored["state"]


async def test_create_permalink_url_params_present_stored_in_state():
    data = ExplorePermalinkCreateSchema(
        form_data={"datasource": "2__table"},
        url_params=[["foo", "bar"]],
    )
    entry = MagicMock()
    entry.id = 2
    fake_dao = AsyncMock()
    fake_dao.create_entry.return_value = entry
    session = AsyncMock()
    create_fn = ExplorePermalinkController.create_permalink.fn
    mock_request = MagicMock()
    mock_request.base_url = "http://localhost:8088/"

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
            return_value="key2",
        ),
        patch.object(
            ExplorePermalinkController.create_permalink.fn.__globals__["event_logger"],
            "alog_with_context",
            new=AsyncMock(),
        ),
    ):
        await create_fn(
            None,
            request=mock_request,
            data=data,
            kv_dao=AsyncMock(),
            chart_dao=AsyncMock(),
            dataset_dao=AsyncMock(),
            query_dao=AsyncMock(),
            current_user=MagicMock(id=1),
            security_manager=MagicMock(),
            session=session,
        )

    call_kwargs = fake_dao.create_entry.call_args.kwargs
    stored = json.loads(call_kwargs["value"].decode("utf-8"))
    assert stored["state"]["urlParams"] == [["foo", "bar"]]


async def test_get_permalink_invalid_key_raises_parse_error(mock_kv_dao, mock_user):
    """An undecodeable permalink key raises KeyValueParseKeyError
    (status_code=500), NOT ObjectNotFoundError (404).

    GetExplorePermalinkCommand.run() wraps KeyValueParseKeyError in
    ExplorePermalinkGetFailedError (also 500), which is not caught by the error
    handler. KeyValueParseKeyError must propagate so the exception handler
    returns 500.
    """
    from superset.exceptions import ObjectNotFoundError
    from superset.key_value.exceptions import KeyValueParseKeyError

    get_fn = ExplorePermalinkController.get_permalink.fn

    with (
        patch(
            "superset.controllers.explore_permalink.get_permalink_salt",
            new=AsyncMock(return_value="salt"),
        ),
        patch(
            "superset.controllers.explore_permalink.decode_permalink_id",
            side_effect=KeyValueParseKeyError("Invalid permalink key"),
        ),
    ):
        with pytest.raises(KeyValueParseKeyError) as exc_info:
            await get_fn(
                None,
                key="!!!invalid!!!",
                kv_dao=mock_kv_dao,
                chart_dao=AsyncMock(),
                dataset_dao=AsyncMock(),
                query_dao=AsyncMock(),
                current_user=mock_user,
                security_manager=MagicMock(),
                session=AsyncMock(),
            )
    assert not isinstance(exc_info.value, ObjectNotFoundError), (
        "decode failure must not be converted to 404 — original returns 500"
    )
    assert exc_info.value.status_code == 500
