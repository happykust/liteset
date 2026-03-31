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

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from superset.controllers.user import UserController, UserRegistrationsController
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
_get_avatar = UserController.get_avatar.fn
_reg_get_list = UserRegistrationsController.get_list.fn
_reg_get_single = UserRegistrationsController.get_single.fn


@pytest.fixture()
def mock_user():
    user = MagicMock()
    type(user).id = PropertyMock(return_value=1)
    type(user).username = PropertyMock(return_value="testuser")
    type(user).is_authenticated = PropertyMock(return_value=True)
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
    assert result["result"]["id"] == 1
    assert result["result"]["is_anonymous"] is False


async def test_get_me_with_roles(mock_user: MagicMock) -> None:
    role = MagicMock()
    role.id = 1
    role.name = "Admin"
    mock_user.roles = [role]
    result = await _get_me(None, current_user=mock_user)
    assert len(result["result"]["roles"]) == 1
    assert result["result"]["roles"][0]["name"] == "Admin"


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
    result = await _get_my_roles(None, current_user=mock_user)
    assert "Admin" in result["result"]["roles"]
    assert result["result"]["roles"]["Admin"] == [("can_read", "Dashboard")]
    assert result["result"]["permissions"] == {"can_read": ["Dashboard"]}
    assert result["result"]["username"] == "testuser"


async def test_get_my_roles_empty(mock_user: MagicMock) -> None:
    result = await _get_my_roles(None, current_user=mock_user)
    assert result["result"]["roles"] == {}
    assert result["result"]["permissions"] == {}


# ---------------------------------------------------------------------------
# CurrentUserController — update_me
# ---------------------------------------------------------------------------


async def test_update_me(mock_user: MagicMock) -> None:
    data = CurrentUserUpdateRequest(first_name="New")
    user_dao = AsyncMock()
    user_dao.find_by_id.return_value = mock_user
    result = await _update_me(None, data=data, current_user=mock_user, user_dao=user_dao)
    assert result["result"]["first_name"] == "New"
    assert "last_name" not in result["result"]


async def test_update_me_both_fields(mock_user: MagicMock) -> None:
    data = CurrentUserUpdateRequest(first_name="New", last_name="Name")
    user_dao = AsyncMock()
    user_dao.find_by_id.return_value = mock_user
    result = await _update_me(None, data=data, current_user=mock_user, user_dao=user_dao)
    assert result["result"]["first_name"] == "New"
    assert result["result"]["last_name"] == "Name"


async def test_update_me_empty(mock_user: MagicMock) -> None:
    data = CurrentUserUpdateRequest()
    user_dao = AsyncMock()
    result = await _update_me(None, data=data, current_user=mock_user, user_dao=user_dao)
    assert result["result"] == {}


# ---------------------------------------------------------------------------
# UserController — get_avatar
# ---------------------------------------------------------------------------


async def test_avatar_not_found() -> None:
    user_dao = AsyncMock()
    user_dao.find_by_id.return_value = None
    with pytest.raises(ObjectNotFoundError):
        await _get_avatar(None, pk=999, user_dao=user_dao)


async def test_avatar_gravatar_fallback() -> None:
    user_dao = AsyncMock()
    user = MagicMock()
    user.email = "test@example.com"
    user.extra_attributes = []
    user_dao.find_by_id.return_value = user
    result = await _get_avatar(None, pk=1, user_dao=user_dao)
    assert "gravatar.com" in result.url


async def test_avatar_custom_url() -> None:
    user_dao = AsyncMock()
    user = MagicMock()
    attr = MagicMock()
    attr.avatar_url = "https://example.com/avatar.png"
    user.extra_attributes = [attr]
    user_dao.find_by_id.return_value = user
    result = await _get_avatar(None, pk=1, user_dao=user_dao)
    assert result.url == "https://example.com/avatar.png"


# ---------------------------------------------------------------------------
# UserRegistrationsController — stubs
# ---------------------------------------------------------------------------


async def test_registrations_list_stub() -> None:
    result = await _reg_get_list(None)
    assert result["result"] == []
    assert result["count"] == 0


async def test_registrations_single_not_found() -> None:
    with pytest.raises(ObjectNotFoundError):
        await _reg_get_single(None, pk=1)


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
