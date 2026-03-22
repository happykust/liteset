"""Tests for current_user dependency activation."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from liteset.dependencies import (
    get_current_user,
    get_user_id,
    get_username,
    provide_security_manager,
)
from liteset.middleware.auth import UnauthenticatedUser


@dataclass
class MockUser:
    id: int = 42
    username: str = "testuser"
    is_authenticated: bool = True


def test_get_current_user_returns_user():
    request = MagicMock()
    request.user = MockUser()
    user = get_current_user(request)
    assert user.id == 42
    assert user.username == "testuser"


def test_get_current_user_returns_none_when_no_user():
    request = MagicMock(spec=[])
    user = get_current_user(request)
    assert user is None


def test_get_user_id():
    request = MagicMock()
    request.user = MockUser(id=7)
    assert get_user_id(request) == 7


def test_get_user_id_no_user():
    request = MagicMock(spec=[])
    assert get_user_id(request) is None


def test_get_username():
    request = MagicMock()
    request.user = MockUser(username="alice")
    assert get_username(request) == "alice"


def test_get_username_no_user():
    request = MagicMock(spec=[])
    assert get_username(request) is None


def test_get_current_user_unauthenticated():
    request = MagicMock()
    request.user = UnauthenticatedUser()
    user = get_current_user(request)
    assert user.is_authenticated is False


async def test_provide_security_manager_passes_settings():
    """SecurityManager DI should pass config settings from LitesetSettings."""
    session = AsyncMock()
    state = MagicMock()
    state.settings.auth_role_admin = "SuperAdmin"
    state.settings.auth_role_public = "Viewer"
    state.settings.guest_role_name = "EmbedGuest"
    state.settings.dashboard_rbac = True

    sm = await provide_security_manager(session, state)
    assert sm._admin_role_name == "SuperAdmin"
    assert sm._public_role_name == "Viewer"
    assert sm._guest_role_name == "EmbedGuest"
    assert sm._dashboard_rbac_enabled is True
