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
"""Tests for User controllers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from superset.controllers.user import (
    UserController,
    UserPublicController,
    UserRegistrationsController,
)
from superset.controllers.user_me import CurrentUserController
from superset.exceptions import ObjectNotFoundError
from superset.schemas.user import (
    CurrentUserResponse,
    CurrentUserUpdateRequest,
    RoleResponseSchema,
)

# Access the original undecorated handler functions via Litestar's .fn property.
_get_me = CurrentUserController.get_me.fn
_get_my_roles = CurrentUserController.get_my_roles.fn
_update_me = CurrentUserController.update_me.fn
# The avatar endpoint moved to the public ``/api/v1/user`` controller.
_get_avatar = UserPublicController.avatar.fn
_reg_get_list = UserRegistrationsController.get_list.fn
_reg_get_single = UserRegistrationsController.get_single.fn


@pytest.fixture
def mock_user():
    user = MagicMock()
    type(user).id = PropertyMock(return_value=1)
    type(user).username = PropertyMock(return_value="testuser")
    type(user).is_authenticated = PropertyMock(return_value=True)
    # MagicMock auto-attrs are truthy; an unset ``is_guest`` would route
    # get_my_roles into the guest branch.
    user.is_guest = False
    user.first_name = "Test"
    user.last_name = "User"
    user.email = "test@example.com"
    user.is_active = True
    user.roles = []
    return user


# ---------------------------------------------------------------------------
# CurrentUserController — get_me
# ---------------------------------------------------------------------------


async def test_get_me(mock_user: MagicMock) -> None:
    result = await _get_me(None, current_user=mock_user)
    assert result["result"]["username"] == "testuser"
    assert result["result"]["is_anonymous"] is False


async def test_get_me_excludes_id_and_roles(mock_user: MagicMock) -> None:
    # ``get_me`` mirrors ``UserResponseSchema``
    # (superset_old/views/users/schemas.py:30-38).
    # That schema includes ``id`` but does NOT include ``roles``
    # (roles are served from ``/me/roles/``).
    role = MagicMock()
    role.id = 1
    role.name = "Admin"
    mock_user.roles = [role]
    result = await _get_me(None, current_user=mock_user)
    assert "id" in result["result"]
    assert result["result"]["id"] == 1
    assert "roles" not in result["result"]


async def test_get_me_anonymous() -> None:
    user = MagicMock()
    type(user).id = PropertyMock(return_value=0)
    type(user).username = PropertyMock(return_value="anonymous")
    type(user).is_authenticated = PropertyMock(return_value=False)
    user.first_name = ""
    user.last_name = ""
    user.email = ""
    user.is_active = False
    user.roles = []
    result = await _get_me(None, current_user=user)
    assert result["result"]["is_anonymous"] is True
    assert result["result"]["is_active"] is False


# ---------------------------------------------------------------------------
# CurrentUserController — get_my_roles
# ---------------------------------------------------------------------------


async def test_get_my_roles(mock_user: MagicMock) -> None:
    role = MagicMock()
    role.name = "Admin"
    pvm = MagicMock()
    pvm.permission.name = "can_read"
    pvm.view_menu.name = "Dashboard"
    role.permissions = [pvm]
    mock_user.roles = [role]
    result = await _get_my_roles(None, current_user=mock_user, user_dao=MagicMock())
    assert "Admin" in result["result"]["roles"]
    assert result["result"]["roles"]["Admin"] == [("can_read", "Dashboard")]
    # ``permissions`` mirrors get_permissions() (superset_old/views/utils.py:143-151):
    # only ``datasource_access`` and ``database_access`` entries are included.
    # "can_read" is neither, so permissions is empty.
    assert result["result"]["permissions"] == {}
    assert result["result"]["username"] == "testuser"


async def test_get_my_roles_empty(mock_user: MagicMock) -> None:
    result = await _get_my_roles(None, current_user=mock_user, user_dao=MagicMock())
    assert result["result"]["roles"] == {}
    assert result["result"]["permissions"] == {}


# ---------------------------------------------------------------------------
# CurrentUserController — update_me
# ---------------------------------------------------------------------------


def _fresh_user(**attrs: Any) -> MagicMock:
    """Build the post-update user that ``update_me`` re-reads via get_by_id."""
    fresh = MagicMock()
    fresh.id = 1
    fresh.username = "testuser"
    fresh.email = "test@example.com"
    fresh.first_name = ""
    fresh.last_name = ""
    fresh.is_active = True
    fresh.login_count = 0
    for key, value in attrs.items():
        setattr(fresh, key, value)
    return fresh


async def test_update_me(mock_user: MagicMock) -> None:
    data = CurrentUserUpdateRequest(first_name="New")
    user_dao = AsyncMock()
    # update_me applies via update_profile then re-reads via get_by_id and
    # serialises the persisted (fresh) state.
    user_dao.get_by_id.return_value = _fresh_user(first_name="New")
    result = await _update_me(
        None, data=data, current_user=mock_user, user_dao=user_dao
    )
    assert result["result"]["first_name"] == "New"
    user_dao.update_profile.assert_awaited_once()


async def test_update_me_both_fields(mock_user: MagicMock) -> None:
    data = CurrentUserUpdateRequest(first_name="New", last_name="Name")
    user_dao = AsyncMock()
    user_dao.get_by_id.return_value = _fresh_user(first_name="New", last_name="Name")
    result = await _update_me(
        None, data=data, current_user=mock_user, user_dao=user_dao
    )
    assert result["result"]["first_name"] == "New"
    assert result["result"]["last_name"] == "Name"


async def test_update_me_empty(mock_user: MagicMock) -> None:
    # An empty payload is rejected with a 400 (upstream ``response_400``).
    from litestar.exceptions import HTTPException

    data = CurrentUserUpdateRequest()
    user_dao = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await _update_me(None, data=data, current_user=mock_user, user_dao=user_dao)
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# update_me — name Length validation (1:1 with upstream Length(1, 64))
# These constraints are applied at decode time by msgspec; we test that the
# schema rejects invalid names before the handler even runs.
# ---------------------------------------------------------------------------


def test_update_request_name_too_short() -> None:
    """Empty string violates min_length=1 on first_name / last_name."""
    import msgspec

    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(b'{"first_name":""}', type=CurrentUserUpdateRequest)


def test_update_request_name_too_long() -> None:
    """A 65-character name violates max_length=64."""
    import msgspec

    long_name = "A" * 65
    payload = f'{{"first_name":"{long_name}"}}'.encode()
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(payload, type=CurrentUserUpdateRequest)


def test_update_request_name_max_boundary() -> None:
    """Exactly 64 characters is accepted."""
    import msgspec

    name_64 = "A" * 64
    payload = f'{{"first_name":"{name_64}"}}'.encode()
    req = msgspec.json.decode(payload, type=CurrentUserUpdateRequest)
    assert req.first_name == name_64


def test_update_request_name_min_boundary() -> None:
    """Exactly 1 character is accepted."""
    import msgspec

    req = msgspec.json.decode(b'{"first_name":"X"}', type=CurrentUserUpdateRequest)
    assert req.first_name == "X"


def test_update_request_last_name_too_short() -> None:
    """Empty last_name violates min_length=1."""
    import msgspec

    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(b'{"last_name":""}', type=CurrentUserUpdateRequest)


def test_update_request_last_name_too_long() -> None:
    """65-char last_name violates max_length=64."""
    import msgspec

    long_name = "B" * 65
    payload = f'{{"last_name":"{long_name}"}}'.encode()
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(payload, type=CurrentUserUpdateRequest)


def test_update_request_omitted_names_ok() -> None:
    """Omitting first_name / last_name entirely is fine (None default)."""
    import msgspec

    req = msgspec.json.decode(
        b'{"password":"somepassword"}', type=CurrentUserUpdateRequest
    )
    assert req.first_name is None
    assert req.last_name is None


# ---------------------------------------------------------------------------
# update_me — password complexity (1:1 with upstream PasswordComplexityValidator)
# The check only fires when FAB_PASSWORD_COMPLEXITY_ENABLED=True.
# ---------------------------------------------------------------------------


async def test_update_me_password_complexity_disabled(mock_user: MagicMock) -> None:
    """When FAB_PASSWORD_COMPLEXITY_ENABLED=False (default), any password passes."""
    data = CurrentUserUpdateRequest(password="weak")
    user_dao = AsyncMock()
    user_dao.get_by_id.return_value = _fresh_user()

    # Patch SupersetSettings so the test is hermetic (no env needed).
    settings_mock = MagicMock()
    settings_mock.fab_password_complexity_enabled = False

    with patch(
        "superset.controllers.user_me.SupersetSettings",
        return_value=settings_mock,
    ):
        result = await _update_me(
            None, data=data, current_user=mock_user, user_dao=user_dao
        )
    assert result["result"] is not None  # didn't raise


async def test_update_me_weak_password_complexity_enabled(mock_user: MagicMock) -> None:
    """Weak password raises 400 when complexity is enabled.

    Mirrors original Superset: ``UserRestApi`` password-complexity failures
    surface as HTTP 400 (not 422).
    """
    from litestar.exceptions import HTTPException

    data = CurrentUserUpdateRequest(password="weak")
    user_dao = AsyncMock()

    settings_mock = MagicMock()
    settings_mock.fab_password_complexity_enabled = True
    settings_mock.fab_password_complexity_validator = None  # use default rules

    with patch(
        "superset.controllers.user_me.SupersetSettings",
        return_value=settings_mock,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _update_me(None, data=data, current_user=mock_user, user_dao=user_dao)
    assert exc_info.value.status_code == 400
    assert "password" in exc_info.value.detail


async def test_update_me_strong_password_complexity_enabled(
    mock_user: MagicMock,
) -> None:
    """A password satisfying all FAB rules passes when complexity is enabled."""
    # Satisfies: ≥2 uppercase, ≥1 special, ≥2 digits, ≥3 lowercase, ≥10 chars
    strong_pw = "ABcde12!fg"

    data = CurrentUserUpdateRequest(password=strong_pw)
    user_dao = AsyncMock()
    user_dao.get_by_id.return_value = _fresh_user()

    settings_mock = MagicMock()
    settings_mock.fab_password_complexity_enabled = True
    settings_mock.fab_password_complexity_validator = None

    with patch(
        "superset.controllers.user_me.SupersetSettings",
        return_value=settings_mock,
    ):
        result = await _update_me(
            None, data=data, current_user=mock_user, user_dao=user_dao
        )
    assert result["result"] is not None


async def test_update_me_omitted_password_complexity_not_checked(
    mock_user: MagicMock,
) -> None:
    """Omitting password entirely skips complexity check (1:1 with upstream)."""
    data = CurrentUserUpdateRequest(first_name="Alice")
    user_dao = AsyncMock()
    user_dao.get_by_id.return_value = _fresh_user(first_name="Alice")

    settings_mock = MagicMock()
    settings_mock.fab_password_complexity_enabled = True
    settings_mock.fab_password_complexity_validator = None

    # SupersetSettings should never even be instantiated for this code path —
    # the password block is guarded by ``if has_password``.
    with patch(
        "superset.controllers.user_me.SupersetSettings",
        return_value=settings_mock,
    ) as patched_cls:
        result = await _update_me(
            None, data=data, current_user=mock_user, user_dao=user_dao
        )
    patched_cls.assert_not_called()
    assert result["result"]["first_name"] == "Alice"


async def test_update_me_custom_complexity_validator_fires(
    mock_user: MagicMock,
) -> None:
    """A custom FAB_PASSWORD_COMPLEXITY_VALIDATOR is called and its exception
    propagates."""
    from litestar.exceptions import HTTPException

    def bad_validator(pw: str) -> None:  # noqa: ARG001
        raise ValueError("custom rule failed")

    data = CurrentUserUpdateRequest(password="anypassword")
    user_dao = AsyncMock()

    settings_mock = MagicMock()
    settings_mock.fab_password_complexity_enabled = True
    settings_mock.fab_password_complexity_validator = bad_validator

    with patch(
        "superset.controllers.user_me.SupersetSettings",
        return_value=settings_mock,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _update_me(None, data=data, current_user=mock_user, user_dao=user_dao)
    assert exc_info.value.status_code == 400
    assert "custom rule failed" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# UserPublicController — avatar
#
# Ported 1:1 from ``UserRestApi.avatar``: loads via ``user_dao.get_by_id``,
# reads ``extra_attributes[0].avatar_url``, optionally falls back to Slack
# (no Gravatar fallback). Returns a 301 ``Redirect`` to the avatar URL, a 204
# ``Response`` when none is found, or a 404 ``Response`` for a missing user.
# ---------------------------------------------------------------------------


def _avatar_state() -> MagicMock:
    """A ``State`` whose settings disable the Slack lookup branch."""
    state = MagicMock()
    state.settings = None  # -> slack_token is None -> Slack branch skipped
    return state


async def test_avatar_not_found() -> None:
    user_dao = AsyncMock()
    user_dao.get_by_id.return_value = None
    result = await _get_avatar(
        None, user_dao=user_dao, user_id=999, state=_avatar_state()
    )
    assert result.status_code == 404


async def test_avatar_none_returns_204() -> None:
    user_dao = AsyncMock()
    user = MagicMock()
    user.email = "test@example.com"
    user.extra_attributes = []
    user_dao.get_by_id.return_value = user
    result = await _get_avatar(
        None, user_dao=user_dao, user_id=1, state=_avatar_state()
    )
    # No custom URL and Slack disabled -> 204 No Content (no Gravatar fallback).
    assert result.status_code == 204


async def test_avatar_custom_url() -> None:
    user_dao = AsyncMock()
    user = MagicMock()
    attr = MagicMock()
    attr.avatar_url = "https://example.com/avatar.png"
    user.extra_attributes = [attr]
    user_dao.get_by_id.return_value = user
    result = await _get_avatar(
        None, user_dao=user_dao, user_id=1, state=_avatar_state()
    )
    # 301 permanent redirect to the stored avatar URL.
    assert result.status_code == 301


# ---------------------------------------------------------------------------
# UserRegistrationsController — stubs
# ---------------------------------------------------------------------------


async def test_registrations_list_empty() -> None:
    # get_list now queries reg_dao.search(...) -> (registrations, total).
    reg_dao = AsyncMock()
    reg_dao.search.return_value = ([], 0)
    result = await _reg_get_list(None, reg_dao=reg_dao, rison_params=None)
    assert result["result"] == []
    assert result["count"] == 0


async def test_registrations_single_not_found() -> None:
    reg_dao = AsyncMock()
    reg_dao.find_by_id.return_value = None
    with pytest.raises(ObjectNotFoundError):
        await _reg_get_single(None, reg_dao=reg_dao, pk=1)


# ---------------------------------------------------------------------------
# Schema unit tests
# ---------------------------------------------------------------------------


def test_current_user_response_schema() -> None:
    resp = CurrentUserResponse(
        id=1,
        username="admin",
        first_name="Admin",
        last_name="User",
        email="admin@example.com",
        is_active=True,
        is_anonymous=False,
        roles=[RoleResponseSchema(id=1, name="Admin")],
    )
    assert resp.username == "admin"
    assert resp.roles[0].name == "Admin"


def test_current_user_update_request_defaults() -> None:
    req = CurrentUserUpdateRequest()
    assert req.first_name is None
    assert req.last_name is None
    assert req.password is None


# ---------------------------------------------------------------------------
# Controller path assertions
# ---------------------------------------------------------------------------


def test_controller_paths() -> None:
    assert CurrentUserController.path == "/api/v1/me"
    assert UserController.path == "/api/v1/security/users"
    assert UserRegistrationsController.path == "/api/v1/security/user_registrations"
