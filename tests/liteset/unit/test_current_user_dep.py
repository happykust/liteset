"""Tests for current_user dependency activation."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from liteset.dependencies import get_current_user, get_user_id, get_username
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
