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

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.controllers.embedded_dashboard import EmbeddedDashboardController


def _get_raw_method(controller_cls: type, method_name: str):
    """Litestar decorators wrap methods; unwrap to the raw fn for unit tests."""
    handler = getattr(controller_cls, method_name)
    if hasattr(handler, "fn"):
        return handler.fn
    return handler


_get_embedded = _get_raw_method(EmbeddedDashboardController, "get_embedded")


@pytest.fixture
def controller():
    return EmbeddedDashboardController(owner=MagicMock())


def test_embedded_disabled(monkeypatch):
    """EMBEDDED_SUPERSET off → controller-level guard raises 404."""
    from litestar.exceptions import NotFoundException

    from superset.utils.feature_flags import feature_flag_manager

    monkeypatch.setattr(
        feature_flag_manager, "is_feature_enabled", lambda feature: False
    )
    assert EmbeddedDashboardController.guards, (
        "controller must carry a feature-flag guard"
    )
    guard = EmbeddedDashboardController.guards[0]
    with pytest.raises(NotFoundException):
        guard(MagicMock(), MagicMock())


def test_embedded_disabled_explicit_false(monkeypatch):
    """Guard consults exactly the EMBEDDED_SUPERSET flag."""
    from litestar.exceptions import NotFoundException

    from superset.utils.feature_flags import feature_flag_manager

    seen: list[str] = []

    def _is_enabled(feature: str) -> bool:
        seen.append(feature)
        return False

    monkeypatch.setattr(feature_flag_manager, "is_feature_enabled", _is_enabled)
    guard = EmbeddedDashboardController.guards[0]
    with pytest.raises(NotFoundException):
        guard(MagicMock(), MagicMock())
    assert seen == ["EMBEDDED_SUPERSET"]


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
    """Upstream ``EmbeddedDashboardRestApi.get`` is ``@protect()`` + can_read;
    the endpoint must NOT be ``exclude_from_auth`` — the previous open
    behaviour leaked embedded config to unauthenticated callers."""
    handler = EmbeddedDashboardController.get_embedded
    opt = getattr(handler, "opt", {})
    assert opt.get("exclude_from_auth") is not True
    assert getattr(handler, "guards", None)
