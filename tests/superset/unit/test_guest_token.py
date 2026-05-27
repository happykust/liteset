"""Tests for GuestUser and guest token creation/validation."""

from __future__ import annotations

import time

import jwt

from superset.security.guest import (
    create_guest_access_token,
    GuestUser,
    parse_guest_token,
)

SECRET_KEY = "test-secret-key-at-least-16-chars"


def test_create_guest_token():
    token = create_guest_access_token(
        secret_key=SECRET_KEY,
        user={"username": "guest", "first_name": "Guest", "last_name": "User"},
        resources=[{"type": "dashboard", "id": "abc-123"}],
        rls=[{"clause": "team_id = 1"}],
    )
    assert isinstance(token, str)
    assert len(token) > 0


def test_parse_guest_token_valid():
    token = create_guest_access_token(
        secret_key=SECRET_KEY,
        user={"username": "guest", "first_name": "G", "last_name": "U"},
        resources=[{"type": "dashboard", "id": "abc-123"}],
        rls=[],
    )
    payload = parse_guest_token(token, SECRET_KEY)
    assert payload is not None
    assert payload["user"]["username"] == "guest"
    assert len(payload["resources"]) == 1
    assert payload["resources"][0]["id"] == "abc-123"


def test_parse_guest_token_invalid_signature():
    token = create_guest_access_token(
        secret_key=SECRET_KEY,
        user={"username": "guest"},
        resources=[],
        rls=[],
    )
    payload = parse_guest_token(token, "wrong-secret-key-at-least-32-bytes!")
    assert payload is None


def test_parse_guest_token_expired():
    token = jwt.encode(
        {
            "user": {"username": "guest"},
            "resources": [],
            "rls_rules": [],
            "type": "guest",
            "exp": int(time.time()) - 3600,  # Expired 1 hour ago
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    payload = parse_guest_token(token, SECRET_KEY)
    assert payload is None


def test_parse_guest_token_wrong_type():
    token = jwt.encode(
        {
            "user": {"username": "guest"},
            "resources": [],
            "rls_rules": [],
            "type": "not_guest",
            "exp": int(time.time()) + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    payload = parse_guest_token(token, SECRET_KEY)
    assert payload is None


def test_parse_guest_token_garbage():
    payload = parse_guest_token("not.a.jwt.token", SECRET_KEY)
    assert payload is None


def test_guest_user_from_token_payload():
    payload = {
        "user": {"username": "guest", "first_name": "G", "last_name": "U"},
        "resources": [{"type": "dashboard", "id": "abc-123"}],
        "rls_rules": [{"clause": "team_id = 1"}],
    }
    guest = GuestUser.from_token_payload(payload)
    assert guest.username == "guest"
    assert guest.is_guest is True
    assert guest.is_authenticated is True
    assert len(guest.resources) == 1
    assert len(guest.rls_rules) == 1


def test_guest_user_has_no_real_id():
    payload = {
        "user": {"username": "guest"},
        "resources": [],
        "rls_rules": [],
    }
    guest = GuestUser.from_token_payload(payload)
    assert guest.id == 0


def test_guest_user_roles_empty():
    payload = {
        "user": {"username": "guest"},
        "resources": [],
        "rls_rules": [],
    }
    guest = GuestUser.from_token_payload(payload)
    assert guest.roles == []


def test_validate_guest_token_resources_schema_valid():
    from superset.security.guest import validate_guest_token_resources_schema

    # 1:1 with superset_old: ``GuestTokenResourceType`` has only DASHBOARD, so
    # "dashboard" is the only supported resource type.
    errors = validate_guest_token_resources_schema(
        [
            {"type": "dashboard", "id": "uuid-1"},
            {"type": "dashboard", "id": "uuid-2"},
        ]
    )
    assert errors == []


def test_validate_guest_token_resources_schema_rejects_chart():
    from superset.security.guest import validate_guest_token_resources_schema

    # "chart" is NOT a supported guest-token resource type upstream.
    errors = validate_guest_token_resources_schema(
        [{"type": "chart", "id": "uuid-2"}]
    )
    assert len(errors) == 1


def test_validate_guest_token_resources_schema_invalid():
    from superset.security.guest import validate_guest_token_resources_schema

    errors = validate_guest_token_resources_schema(
        [
            {"type": "invalid_type", "id": "uuid-1"},
            {"id": "missing-type"},
            {"type": "dashboard"},
            "not-a-dict",
        ]
    )
    assert len(errors) == 4
