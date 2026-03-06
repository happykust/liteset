import pytest
from dataclasses import dataclass, field
from unittest.mock import MagicMock

from litestar.connection import ASGIConnection
from litestar.exceptions import PermissionDeniedException

from liteset.guards.rbac import has_permissions, require_permission


@dataclass
class MockUser:
    username: str = "admin"
    is_authenticated: bool = True
    permissions: set = field(default_factory=lambda: {"can_read_Chart", "can_write_Chart"})


@dataclass
class MockLimitedUser:
    username: str = "viewer"
    is_authenticated: bool = True
    permissions: set = field(default_factory=lambda: {"can_read_Chart"})


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
    assert has_permissions(user, {"can_read_Chart"}) is True

def test_has_permissions_false():
    user = MockLimitedUser()
    assert has_permissions(user, {"can_write_Chart"}) is False

def test_has_permissions_multiple():
    user = MockUser()
    assert has_permissions(user, {"can_read_Chart", "can_write_Chart"}) is True

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
    with pytest.raises(PermissionDeniedException, match="can_write_Chart"):
        guard(conn, handler)


def test_require_permission_denies_unauthenticated_user():
    guard = require_permission("can_read", "Chart")
    conn = _make_mock_connection(MockAnonymousUser())
    handler = MagicMock()
    with pytest.raises(PermissionDeniedException, match="Not authenticated"):
        guard(conn, handler)
