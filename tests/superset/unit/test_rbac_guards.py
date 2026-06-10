from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException

from superset.guards.rbac import has_permissions, require_permission


@dataclass
class MockUser:
    username: str = "admin"
    is_authenticated: bool = True
    permissions: set = field(
        default_factory=lambda: {("can_read", "Chart"), ("can_write", "Chart")}
    )


@dataclass
class MockLimitedUser:
    username: str = "viewer"
    is_authenticated: bool = True
    permissions: set = field(default_factory=lambda: {("can_read", "Chart")})


@dataclass
class MockAnonymousUser:
    username: str = "anon"
    is_authenticated: bool = False
    permissions: set = field(default_factory=set)


def _make_mock_connection(user: MockUser | MockLimitedUser) -> MagicMock:
    conn = MagicMock(spec=ASGIConnection)
    conn.user = user
    return conn


def test_has_permissions_true():
    user = MockUser()
    assert has_permissions(user, {("can_read", "Chart")}) is True


def test_has_permissions_false():
    user = MockLimitedUser()
    assert has_permissions(user, {("can_write", "Chart")}) is False


def test_has_permissions_multiple():
    user = MockUser()
    assert (
        has_permissions(user, {("can_read", "Chart"), ("can_write", "Chart")}) is True
    )


def test_has_permissions_empty_required():
    user = MockLimitedUser()
    assert has_permissions(user, set()) is True


def test_require_permission_returns_callable():
    guard = require_permission("can_read", "Chart")
    assert callable(guard)


def test_require_permission_allows_authorized_user():
    guard = require_permission("can_read", "Chart")
    conn = _make_mock_connection(MockUser())
    handler = MagicMock()
    guard(conn, handler)


def test_require_permission_denies_unauthorized_user():
    guard = require_permission("can_write", "Chart")
    conn = _make_mock_connection(MockLimitedUser())
    handler = MagicMock()
    with pytest.raises(PermissionDeniedException, match="can_write on Chart"):
        guard(conn, handler)


def test_require_permission_denies_unauthenticated_user():
    guard = require_permission("can_read", "Chart")
    conn = _make_mock_connection(MockAnonymousUser())
    handler = MagicMock()
    with pytest.raises(NotAuthorizedException, match="Not authenticated"):
        guard(conn, handler)


def test_require_permission_allows_anonymous_user_with_public_role_permission():
    """Anonymous user whose Public role carries the required permission is allowed.

    1:1 with the original FAB ``@protect()`` which calls
    ``sm.is_item_public(permission_str, class_permission_name)`` BEFORE
    checking authentication — so a Public-role grant passes anonymous callers
    through (see Flask-AppBuilder security/decorators.py lines 98-101).
    This behaviour is present in AsyncEventsRestApi which uses ``@protect()``
    with ``allow_browser_login = True``.
    """
    user = MockAnonymousUser()
    user.permissions = {("can_list", "AsyncEventsRestApi")}
    guard = require_permission("can_list", "AsyncEventsRestApi")
    conn = _make_mock_connection(user)
    handler = MagicMock()
    guard(conn, handler)  # must NOT raise — Public role permission allows anonymous
