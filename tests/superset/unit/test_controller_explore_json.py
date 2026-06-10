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
"""Unit tests for the legacy ``/superset/explore_json`` controller.

Covers the four behaviours the port must preserve 1:1 with the original
Flask ``Superset.explore_json`` / ``Superset.explore_json_data`` views:

* GAQ async branch: GAQ enabled + JSON + valid ``async-token`` cookie →
  ``202`` with job_metadata whose ``channel_id`` equals the cookie channel,
  and ``load_explore_json_into_cache.delay`` called with
  ``(job_metadata, form_data, response_type, force)``.
* GAQ async branch: missing / invalid cookie → ``401``.
* ``explore_json_data``: cache hit returns the viz JSON; cache miss → ``404``.
* Sync JSON path: returns the serialized viz payload.
* Route registration: the three legacy paths resolve.

Handlers are driven directly (their Litestar ``.fn``) with mocked DI
dependencies, mirroring ``test_async_chart_data_channel.py``.  The
``explore_json_data`` cache loader (``load_cached_explore_form``) is
tested separately in ``test_explore_json_cache_loader.py``, including the
pickle round-trip that matches the Celery task's on-disk format.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from litestar.exceptions import NotAuthorizedException

from superset.async_events.manager import extract_guest_token
from superset.controllers.explore_json import (
    _resolve_response_type,
    ExploreJsonController,
    get_datasource_info,
)
from superset.exceptions import SupersetException

GAQ_SECRET = "test-gaq-secret-at-least-16-chars"
COOKIE_NAME = "async-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_raw_method(controller_cls: type, method_name: str) -> Any:
    """Return the underlying async function from a Litestar handler."""
    handler = getattr(controller_cls, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


def _mint_async_token(channel: str, secret: str = GAQ_SECRET) -> str:
    token = pyjwt.encode({"channel": channel, "sub": "1"}, secret, algorithm="HS256")
    return token.decode("ascii") if isinstance(token, bytes) else token


def _cookie_header_bytes(channel: str, secret: str = GAQ_SECRET) -> list[tuple]:
    token = _mint_async_token(channel, secret)
    raw = f"{COOKIE_NAME}={token}"
    return [(b"cookie", raw.encode("utf-8"))]


def _make_request(
    headers: list[tuple],
    *,
    query: dict[str, str] | None = None,
    form_data: dict[str, Any] | None = None,
) -> MagicMock:
    """Mock a Litestar request exposing scope headers, query params and form."""
    request = MagicMock()
    request.scope = {"headers": headers}
    request.content_type = ("application/x-www-form-urlencoded",)
    request.query_params = query or {}

    form: dict[str, str] = {}
    if form_data is not None:
        form["form_data"] = json.dumps(form_data)
    request.form = AsyncMock(return_value=form)
    request.json = AsyncMock(side_effect=ValueError("not json"))
    return request


_explore_json_post = _get_raw_method(ExploreJsonController, "explore_json_post")
_explore_json_get = _get_raw_method(ExploreJsonController, "explore_json_get")
_explore_json_data = _get_raw_method(ExploreJsonController, "explore_json_data")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def controller() -> ExploreJsonController:
    return ExploreJsonController(owner=MagicMock())


@pytest.fixture
def mock_user() -> MagicMock:
    user = MagicMock()
    user.id = 1
    user.is_authenticated = True
    return user


@pytest.fixture
def mock_security_manager() -> MagicMock:
    sm = MagicMock()
    sm.can_access = AsyncMock(return_value=True)
    sm.raise_for_access = AsyncMock(return_value=None)
    # Regular (non-guest) user: the GAQ submit branch must NOT forward a
    # guest token. (A bare MagicMock would return a truthy is_guest_user.)
    sm.is_guest_user = MagicMock(return_value=False)
    sm.get_rls_cache_key = AsyncMock(return_value=[])
    return sm


@pytest.fixture
def gaq_state() -> MagicMock:
    """State whose settings enable GAQ and carry the GAQ secret/cookie name."""
    state = MagicMock()
    settings = MagicMock()
    settings.global_async_queries = True
    settings.global_async_queries_jwt_secret = GAQ_SECRET
    settings.global_async_queries_jwt_cookie_name = COOKIE_NAME
    # No data cache configured -> build_sync_viz_cache returns None (the viz
    # cache wiring is a no-op); control flow is what these tests assert.
    settings.data_cache_config = None
    settings.redis_url = None
    state.settings = settings
    return state


@pytest.fixture
def sync_state() -> MagicMock:
    """State whose settings disable GAQ (forces the sync branch)."""
    state = MagicMock()
    settings = MagicMock()
    settings.global_async_queries = False
    settings.data_cache_config = None
    settings.redis_url = None
    state.settings = settings
    return state


@pytest.fixture
def form_data() -> dict[str, Any]:
    return {
        "datasource": "1__table",
        "viz_type": "table",
        "metrics": ["count"],
    }


def _patch_jsctrl_off() -> Any:
    """Disable ENABLE_JAVASCRIPT_CONTROLS so the rejected-keys filter is inert."""
    return patch(
        "superset.utils.feature_flags.feature_flag_manager.is_feature_enabled",
        return_value=False,
    )


def _patch_datasource(datasource: Any) -> Any:
    """Patch the controller's datasource loader to return *datasource*."""
    return patch.object(
        ExploreJsonController,
        "_load_datasource",
        new=AsyncMock(return_value=datasource),
    )


# ---------------------------------------------------------------------------
# Pure-helper unit tests
# ---------------------------------------------------------------------------


def test_get_datasource_info_from_form_data() -> None:
    ds_id, ds_type = get_datasource_info(None, None, {"datasource": "7__table"})
    assert ds_id == 7
    assert ds_type == "table"


def test_get_datasource_info_url_args_fallback() -> None:
    ds_id, ds_type = get_datasource_info(9, "table", {})
    assert ds_id == 9
    assert ds_type == "table"


def test_get_datasource_info_deleted_dataset_raises() -> None:
    with pytest.raises(SupersetException):
        get_datasource_info(None, None, {"datasource": "None__table"})


def test_get_datasource_info_missing_raises() -> None:
    with pytest.raises(SupersetException):
        get_datasource_info(None, None, {})


def test_resolve_response_type_default_json() -> None:
    req = MagicMock()
    req.query_params = {}
    assert _resolve_response_type(req) == "json"


@pytest.mark.parametrize("flag", ["csv", "query", "results", "samples", "xlsx"])
def test_resolve_response_type_flags(flag: str) -> None:
    req = MagicMock()
    req.query_params = {flag: "true"}
    assert _resolve_response_type(req) == flag


# ---------------------------------------------------------------------------
# GAQ async branch
# ---------------------------------------------------------------------------


async def test_async_branch_submits_with_cookie_channel(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    gaq_state: MagicMock,
    form_data: dict[str, Any],
) -> None:
    """GAQ + JSON + valid cookie → 202; delay called with the 4-arg tuple."""
    channel = str(uuid.uuid4())
    datasource = MagicMock()

    request = _make_request(
        _cookie_header_bytes(channel),
        query={},
        form_data=form_data,
    )

    with (
        _patch_jsctrl_off(),
        _patch_datasource(datasource),
        patch("superset.tasks.async_queries.load_explore_json_into_cache") as mock_task,
        patch("superset.viz.get_viz") as mock_get_viz,
    ):
        # Cache-first miss: get_payload returns None so the submit path runs.
        viz_obj = MagicMock()
        viz_obj.get_payload = AsyncMock(return_value=None)
        mock_get_viz.return_value = viz_obj
        mock_task.delay = MagicMock()

        result = await _explore_json_post(
            controller,
            request=request,
            session=AsyncMock(),
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=gaq_state,
            datasource_type=None,
            datasource_id=None,
        )

    assert result.status_code == 202
    assert result.content["channel_id"] == channel
    assert result.content["status"] == "pending"
    assert result.content["user_id"] == 1
    assert set(result.content) == {
        "channel_id",
        "job_id",
        "user_id",
        "status",
        "errors",
        "result_url",
    }

    mock_task.delay.assert_called_once()
    job_metadata, sent_form_data, response_type, force = mock_task.delay.call_args.args
    assert job_metadata["channel_id"] == channel
    assert sent_form_data == form_data
    assert response_type == "json"
    assert force is False
    # Non-guest user → no guest token forwarded to the worker.
    assert "guest_token" not in job_metadata


async def test_async_branch_cache_hit_returns_payload(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    gaq_state: MagicMock,
    form_data: dict[str, Any],
) -> None:
    """GAQ cache-first hit short-circuits and returns the payload (no delay)."""
    channel = str(uuid.uuid4())
    datasource = MagicMock()

    request = _make_request(
        _cookie_header_bytes(channel),
        query={},
        form_data=form_data,
    )

    with (
        _patch_jsctrl_off(),
        _patch_datasource(datasource),
        patch("superset.tasks.async_queries.load_explore_json_into_cache") as mock_task,
        patch("superset.viz.get_viz") as mock_get_viz,
    ):
        viz_obj = MagicMock()
        viz_obj.get_payload = AsyncMock(return_value={"data": [{"x": 1}]})
        viz_obj.payload_json_and_has_error = MagicMock(
            return_value=('{"data": [{"x": 1}]}', False)
        )
        mock_get_viz.return_value = viz_obj
        mock_task.delay = MagicMock()

        result = await _explore_json_post(
            controller,
            request=request,
            session=AsyncMock(),
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=gaq_state,
            datasource_type=None,
            datasource_id=None,
        )

    assert result.status_code == 200
    assert result.content == '{"data": [{"x": 1}]}'
    mock_task.delay.assert_not_called()


async def test_async_branch_missing_cookie_401(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    gaq_state: MagicMock,
    form_data: dict[str, Any],
) -> None:
    """GAQ + JSON but no async-token cookie → 401 (no delay)."""
    datasource = MagicMock()
    request = _make_request([], query={}, form_data=form_data)

    with (
        _patch_jsctrl_off(),
        _patch_datasource(datasource),
        patch("superset.tasks.async_queries.load_explore_json_into_cache") as mock_task,
        patch("superset.viz.get_viz") as mock_get_viz,
    ):
        viz_obj = MagicMock()
        viz_obj.get_payload = AsyncMock(return_value=None)
        mock_get_viz.return_value = viz_obj
        mock_task.delay = MagicMock()

        with pytest.raises(NotAuthorizedException):
            await _explore_json_post(
                controller,
                request=request,
                session=AsyncMock(),
                security_manager=mock_security_manager,
                current_user=mock_user,
                state=gaq_state,
                datasource_type=None,
                datasource_id=None,
            )
        mock_task.delay.assert_not_called()


async def test_async_branch_wrong_secret_401(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    gaq_state: MagicMock,
    form_data: dict[str, Any],
) -> None:
    """GAQ + JSON but cookie signed with a bad key → 401 (no delay)."""
    datasource = MagicMock()
    request = _make_request(
        _cookie_header_bytes(str(uuid.uuid4()), secret="a-different-wrong-secret-key"),
        query={},
        form_data=form_data,
    )

    with (
        _patch_jsctrl_off(),
        _patch_datasource(datasource),
        patch("superset.tasks.async_queries.load_explore_json_into_cache") as mock_task,
        patch("superset.viz.get_viz") as mock_get_viz,
    ):
        viz_obj = MagicMock()
        viz_obj.get_payload = AsyncMock(return_value=None)
        mock_get_viz.return_value = viz_obj
        mock_task.delay = MagicMock()

        with pytest.raises(NotAuthorizedException):
            await _explore_json_post(
                controller,
                request=request,
                session=AsyncMock(),
                security_manager=mock_security_manager,
                current_user=mock_user,
                state=gaq_state,
                datasource_type=None,
                datasource_id=None,
            )
        mock_task.delay.assert_not_called()


async def test_extract_guest_token_from_header() -> None:
    """The raw guest JWT is re-read from the configured header."""
    settings = MagicMock()
    settings.guest_token_header_name = "X-GuestToken"
    request = MagicMock()
    request.headers = {"X-GuestToken": "raw-guest-jwt"}
    assert await extract_guest_token(request, settings) == "raw-guest-jwt"


async def test_maybe_forward_guest_token_guest_merges() -> None:
    """For a guest user the shared helper merges the raw token into metadata."""
    from superset.async_events.manager import maybe_forward_guest_token

    settings = MagicMock()
    settings.guest_token_header_name = "X-GuestToken"
    request = MagicMock()
    request.headers = {"X-GuestToken": "raw-guest-jwt"}
    sm = MagicMock()
    sm.is_guest_user = MagicMock(return_value=True)
    out = await maybe_forward_guest_token(
        {"channel_id": "c", "job_id": "j"},
        request=request,
        settings=settings,
        security_manager=sm,
        current_user=MagicMock(),
    )
    assert out["guest_token"] == "raw-guest-jwt"


async def test_maybe_forward_guest_token_non_guest_noop() -> None:
    """For a non-guest user the metadata is returned unchanged."""
    from superset.async_events.manager import maybe_forward_guest_token

    sm = MagicMock()
    sm.is_guest_user = MagicMock(return_value=False)
    meta = {"channel_id": "c", "job_id": "j"}
    out = await maybe_forward_guest_token(
        meta,
        request=MagicMock(),
        settings=MagicMock(),
        security_manager=sm,
        current_user=MagicMock(),
    )
    assert out == meta
    assert "guest_token" not in out


async def test_async_branch_forwards_guest_token(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    gaq_state: MagicMock,
    form_data: dict[str, Any],
) -> None:
    """GAQ submit forwards the raw guest token so the worker matches RLS keys."""
    gaq_state.settings.guest_token_header_name = "X-GuestToken"
    channel = str(uuid.uuid4())
    datasource = MagicMock()
    request = _make_request(
        _cookie_header_bytes(channel), query={}, form_data=form_data
    )
    request.headers = {"X-GuestToken": "raw-guest-jwt"}

    sm = MagicMock()
    sm.raise_for_access = AsyncMock(return_value=None)
    sm.is_guest_user = MagicMock(return_value=True)

    with (
        _patch_jsctrl_off(),
        _patch_datasource(datasource),
        patch("superset.tasks.async_queries.load_explore_json_into_cache") as mock_task,
        patch("superset.viz.get_viz") as mock_get_viz,
    ):
        viz_obj = MagicMock()
        viz_obj.get_payload = AsyncMock(return_value=None)
        mock_get_viz.return_value = viz_obj
        mock_task.delay = MagicMock()

        result = await _explore_json_post(
            controller,
            request=request,
            session=AsyncMock(),
            security_manager=sm,
            current_user=mock_user,
            state=gaq_state,
            datasource_type=None,
            datasource_id=None,
        )

    assert result.status_code == 202
    # The token rides only on the *dispatched* metadata...
    dispatched = mock_task.delay.call_args.args[0]
    assert dispatched.get("guest_token") == "raw-guest-jwt"
    # ...never echoed back in the 202 response body.
    assert "guest_token" not in result.content


# ---------------------------------------------------------------------------
# Sync JSON path
# ---------------------------------------------------------------------------


async def test_sync_json_path_returns_payload(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    sync_state: MagicMock,
    form_data: dict[str, Any],
) -> None:
    """GAQ disabled → sync branch builds viz and returns serialized JSON."""
    datasource = MagicMock()
    request = _make_request([], query={}, form_data=form_data)

    with (
        _patch_jsctrl_off(),
        _patch_datasource(datasource),
        patch("superset.viz.get_viz") as mock_get_viz,
    ):
        viz_obj = MagicMock()
        viz_obj.get_payload = AsyncMock(return_value={"data": [{"a": 2}]})
        viz_obj.payload_json_and_has_error = MagicMock(
            return_value=('{"data": [{"a": 2}]}', False)
        )
        mock_get_viz.return_value = viz_obj

        result = await _explore_json_get(
            controller,
            request=request,
            session=AsyncMock(),
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=sync_state,
            datasource_type=None,
            datasource_id=None,
        )

    assert result.status_code == 200
    assert result.content == '{"data": [{"a": 2}]}'
    # Datasource read access was enforced.
    mock_security_manager.raise_for_access.assert_awaited_once()


async def test_sync_csv_no_permission_403(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    sync_state: MagicMock,
    form_data: dict[str, Any],
) -> None:
    """CSV response type without can_csv permission → 403."""
    sm = MagicMock()
    sm.can_access = AsyncMock(return_value=False)
    sm.raise_for_access = AsyncMock(return_value=None)

    request = _make_request([], query={"csv": "true"}, form_data=form_data)

    result = await _explore_json_get(
        controller,
        request=request,
        session=AsyncMock(),
        security_manager=sm,
        current_user=mock_user,
        state=sync_state,
        datasource_type=None,
        datasource_id=None,
    )

    assert result.status_code == 403


async def test_sync_missing_datasource_400(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    sync_state: MagicMock,
) -> None:
    """No resolvable datasource in form_data → 400 (deleted-dataset message)."""
    request = _make_request([], query={}, form_data={"viz_type": "table"})

    with _patch_jsctrl_off():
        result = await _explore_json_get(
            controller,
            request=request,
            session=AsyncMock(),
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=sync_state,
            datasource_type=None,
            datasource_id=None,
        )

    assert result.status_code == 400


# ---------------------------------------------------------------------------
# explore_json_data (cached result fetch)
# ---------------------------------------------------------------------------


async def test_explore_json_data_cache_hit(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    sync_state: MagicMock,
) -> None:
    """Cache hit rebuilds the viz (force_cached) and returns the JSON.

    ``load_cached_explore_form`` accepts the cached value either as a plain
    dict (returned directly) or as pickle bytes; here we return the dict
    directly. The pickle-bytes path is covered by
    ``test_explore_json_cache_loader.py``.
    """
    cache_value = {
        "form_data": {"datasource": "1__table", "viz_type": "table"},
        "response_type": "json",
    }
    datasource = MagicMock()

    cache_slot = MagicMock()
    cache_slot.get = AsyncMock(return_value=cache_value)
    fake_cache_manager = MagicMock()
    fake_cache_manager.cache = cache_slot

    with (
        _patch_datasource(datasource),
        patch("superset.extensions.cache_manager", fake_cache_manager),
        patch("superset.viz.get_viz") as mock_get_viz,
    ):
        viz_obj = MagicMock()
        viz_obj.get_payload = AsyncMock(return_value={"data": []})
        viz_obj.payload_json_and_has_error = MagicMock(
            return_value=('{"data": []}', False)
        )
        mock_get_viz.return_value = viz_obj

        result = await _explore_json_data(
            controller,
            cache_key="ejr-abc123",
            session=AsyncMock(),
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=sync_state,
        )

    assert result.status_code == 200
    assert result.content == '{"data": []}'
    # force_cached=True was passed to get_viz (cache hit, no re-execution).
    assert mock_get_viz.call_args.kwargs.get("force_cached") is True


async def test_explore_json_data_cache_miss_404(
    controller: ExploreJsonController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    sync_state: MagicMock,
) -> None:
    """Cache miss → 404."""
    cache_slot = MagicMock()
    cache_slot.get = AsyncMock(return_value=None)
    fake_cache_manager = MagicMock()
    fake_cache_manager.cache = cache_slot

    with patch("superset.extensions.cache_manager", fake_cache_manager):
        result = await _explore_json_data(
            controller,
            cache_key="ejr-missing",
            session=AsyncMock(),
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=sync_state,
        )

    assert result.status_code == 404


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def test_routes_resolve() -> None:
    """The three legacy paths resolve on the built Litestar app."""
    import os

    os.environ.setdefault("SUPERSET_SECRET_KEY", "not-a-secret-but-long-enough")
    os.environ.setdefault("LITESET_SQLALCHEMY_DATABASE_URI", "sqlite://")

    from superset.app import create_app

    app = create_app()
    paths = {
        getattr(r, "path", "")
        for r in app.routes
        if "explore_json" in getattr(r, "path", "")
    }
    assert "/superset/explore_json" in paths
    assert "/superset/explore_json/{datasource_type:str}/{datasource_id:int}" in paths
    assert "/superset/explore_json/data/{cache_key:str}" in paths


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
