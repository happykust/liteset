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
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

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
    # form_data is required; absent url_params stays UNSET (omitted from the
    # stored state) — explicit null is a distinct, stored value
    # (allow_none=True upstream).
    body = ExplorePermalinkCreateSchema(form_data={})
    assert body.form_data == {}
    assert body.url_params is msgspec.UNSET


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
    # urlParams items are 2-tuples (1:1 with upstream fields.Tuple); msgspec
    # decodes the JSON pair into a tuple.
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
    """A urlParams item that is not a 2-element pair must be rejected
    (upstream fields.Tuple enforces exactly two elements)."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert(
            {"formData": {}, "urlParams": [["a", "b", "c"]]},
            ExplorePermalinkCreateSchema,
        )


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

    # 1:1 with upstream url_for(_external=True): create_permalink builds an
    # absolute URL using request.base_url.  Supply a minimal mock for that.
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
    # create_permalink returns an absolute URL (1:1 with Flask url_for(_external=True)).
    assert "/superset/explore/p/" in result["url"]
    fake_dao.create_entry.assert_awaited_once()


async def test_get_permalink_found(mock_kv_dao, mock_user):
    entry = MagicMock()
    entry.value = json.dumps(
        {"chartId": 1, "datasourceId": 1, "datasourceType": "table"}
    ).encode("utf-8")
    # Non-expiring permalink: ``get_permalink`` treats an entry as missing (404)
    # when ``expires_on`` is in the past (mirrors ``KeyValueEntry.is_expired()``).
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


# ---------------------------------------------------------------------------
# Finding 1 (false_positive): POST with missing/malformed datasource
# ---------------------------------------------------------------------------
# The original superset_old/commands/explore/permalink/create.py accessed
# state["formData"]["datasource"] directly in __init__ (KeyError → 500) and
# split("__") in run() (ValueError → 500).  The liteset guard raises
# CommandInvalidError → 422 instead.  This is NOT a regression: the original
# 500 was an unintentional exception leak for invalid client input; 422 is the
# correct HTTP status.  Tests below assert the CURRENT liteset behavior.


async def test_create_permalink_missing_datasource_raises_command_invalid():
    """POST with formData missing 'datasource' key raises CommandInvalidError.

    Original (superset_old) returned HTTP 500 via unhandled KeyError.
    Liteset returns 422 (CommandInvalidError) — more correct for client error.
    """
    from superset.exceptions import CommandInvalidError

    data = ExplorePermalinkCreateSchema(
        form_data={"viz_type": "table"}  # datasource key absent
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


async def test_create_permalink_malformed_datasource_raises_command_invalid():
    """POST with datasource lacking '__' separator raises CommandInvalidError.

    Original (superset_old) returned HTTP 500 via unhandled ValueError from
    split("__") in run().  Liteset catches it earlier → 422.
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


# ---------------------------------------------------------------------------
# Finding 2 (false_positive): urlParams schema and state-storage shape
# ---------------------------------------------------------------------------
# Original ExplorePermalinkStateSchema used fields.List(fields.Tuple(...))
# which enforces exactly-2-element tuples and allows None string values.
# Liteset uses list[list[str]] | None = None (msgspec Struct).
#
# (a) Over-length inner lists [[k,v,extra]]: accepted by liteset, rejected by
#     original.  Frontend never sends over-length lists in practice.
# (b) Null tuple elements [[k, null]]: original accepts; liteset rejects
#     (msgspec str type disallows None).  Frontend never sends null elements.
# (c) urlParams: null — msgspec cannot distinguish absent from explicit null
#     (both produce url_params=None).  Both cases produce the same stored state.
#     This is an inherent msgspec/Marshmallow migration artifact.


async def test_create_permalink_url_params_none_omitted_from_state():
    """When url_params is None, 'urlParams' is omitted from the stored state.

    Original Marshmallow: absent field → key excluded; explicit null → included
    as None.  msgspec cannot distinguish the two cases (both → None → omitted).
    For the common case (field absent), behavior is identical to original.
    """
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
    # urlParams must NOT appear in state when url_params is None
    assert "urlParams" not in stored["state"]


async def test_create_permalink_url_params_present_stored_in_state():
    """When url_params is provided, urlParams is included in the stored state.

    Original: Marshmallow includes urlParams when the field is present.
    Liteset: url_params is not None → stored in state['urlParams'].
    Behavior is identical for the normal (non-null) case.
    """
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
    """1:1 with original: an undecodeable permalink key raises KeyValueParseKeyError
    (status_code=500), NOT ObjectNotFoundError (404).

    The original GetExplorePermalinkCommand.run() wraps KeyValueParseKeyError in
    ExplorePermalinkGetFailedError (also 500), which is NOT caught by the Flask
    handler, so @safe returns HTTP 500. The liteset port must let
    KeyValueParseKeyError propagate so superset_exception_handler returns 500.
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
        # Must raise KeyValueParseKeyError (status 500), NOT ObjectNotFoundError (404)
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
    # KeyValueParseKeyError is a SupersetException with status_code=500;
    # it must NOT have been swallowed into a 404 ObjectNotFoundError.
    assert not isinstance(exc_info.value, ObjectNotFoundError), (
        "decode failure must not be converted to 404 — original returns 500"
    )
    assert exc_info.value.status_code == 500
