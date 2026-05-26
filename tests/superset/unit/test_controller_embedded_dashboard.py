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
"""Tests for EmbeddedDashboardController."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.controllers.embedded_dashboard import EmbeddedDashboardController
from superset.exceptions import SupersetNotFoundError

# ---------------------------------------------------------------------------
# Helpers — Litestar decorators wrap methods; access the raw fn for unit tests.
# ---------------------------------------------------------------------------


def _get_raw_method(controller_cls: type, method_name: str):
    """Return the underlying async function from a Litestar-decorated controller
    method.
    """
    handler = getattr(controller_cls, method_name)
    if hasattr(handler, "fn"):
        return handler.fn
    return handler


_get_embedded = _get_raw_method(EmbeddedDashboardController, "get_embedded")


@pytest.fixture
def controller():
    return EmbeddedDashboardController(owner=MagicMock())


async def test_embedded_disabled(controller):
    state = MagicMock()
    state.settings.feature_flags = {}
    with pytest.raises(SupersetNotFoundError, match="not enabled"):
        await _get_embedded(
            controller, uuid="test-uuid", state=state, embedded_dao=MagicMock()
        )


async def test_embedded_disabled_explicit_false(controller):
    state = MagicMock()
    state.settings.feature_flags = {"EMBEDDED_SUPERSET": False}
    with pytest.raises(SupersetNotFoundError, match="not enabled"):
        await _get_embedded(
            controller, uuid="test-uuid", state=state, embedded_dao=MagicMock()
        )


async def test_embedded_enabled(controller):
    state = MagicMock()
    state.settings.feature_flags = {"EMBEDDED_SUPERSET": True}
    mock_dao = MagicMock()
    mock_embedded = MagicMock()
    mock_embedded.uuid = "test-uuid"
    mock_embedded.dashboard_id = 1
    mock_embedded.allow_domain_list = None
    mock_dao.find_by_uuid = AsyncMock(return_value=mock_embedded)
    result = await _get_embedded(
        controller, uuid="test-uuid", state=state, embedded_dao=mock_dao
    )
    assert result["result"]["uuid"] == "test-uuid"
    assert result["result"]["allowed_domains"] == []


async def test_embedded_returns_correct_uuid(controller):
    state = MagicMock()
    state.settings.feature_flags = {"EMBEDDED_SUPERSET": True}
    mock_dao = MagicMock()
    mock_embedded = MagicMock()
    mock_embedded.uuid = "550e8400-e29b-41d4-a716-446655440000"
    mock_embedded.dashboard_id = 1
    mock_embedded.allow_domain_list = None
    mock_dao.find_by_uuid = AsyncMock(return_value=mock_embedded)
    result = await _get_embedded(
        controller,
        uuid="550e8400-e29b-41d4-a716-446655440000",
        state=state,
        embedded_dao=mock_dao,
    )
    assert result["result"]["uuid"] == "550e8400-e29b-41d4-a716-446655440000"


def test_controller_path():
    assert EmbeddedDashboardController.path == "/api/v1/embedded_dashboard"


def test_controller_tags():
    assert "Embedded Dashboard" in EmbeddedDashboardController.tags


def test_endpoint_requires_auth():
    """The get_embedded API endpoint must require ``can_read EmbeddedDashboard``.

    Upstream ``EmbeddedDashboardRestApi.get`` is ``@protect()`` + can_read, so
    the JSON config endpoint must NOT be ``exclude_from_auth`` and must carry an
    RBAC guard (the previous open behaviour leaked embedded config anonymously).
    """
    handler = EmbeddedDashboardController.get_embedded
    opt = getattr(handler, "opt", {})
    assert opt.get("exclude_from_auth") is not True
    assert getattr(handler, "guards", None)
