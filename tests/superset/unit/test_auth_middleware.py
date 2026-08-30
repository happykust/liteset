"""Tests for AuthMiddleware — cookie, JWT, and API key authentication."""

from __future__ import annotations

import hashlib
import logging
import json
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from itsdangerous import URLSafeTimedSerializer
from litestar import get, Litestar
from litestar.connection import Request
from litestar.datastructures import State
from litestar.testing import AsyncTestClient

from superset.middleware.auth import (
    CachedUser,
    SupersetAuthMiddleware,
)

SECRET_KEY = "test-secret-key-at-least-16-chars"


def _signed_cache_entry(
    payload: dict,
    secret: str = SECRET_KEY,
    cache_key: str | None = None,
) -> str:
    """Build a Redis auth-cache envelope the middleware will accept.

    Entries are HMAC-signed -- over both the payload *and* the Redis key it
    is stored under -- so that Redis write access alone cannot grant
    permissions, and a legitimately-signed entry cannot be copied onto a
    different user's cache key (``COPY auth:user:1 auth:user:42``).  By
    default *cache_key* is derived from ``payload["id"]`` to match how
    ``_cache_user`` writes real entries; pass it explicitly to simulate a
    transplanted entry.
    """
    from superset.security.auth_cache import sign_keyed_payload

    body = json.dumps(payload)
    key = cache_key if cache_key is not None else f"auth:user:{payload.get('id')}"
    return json.dumps({"sig": sign_keyed_payload(key, body, secret), "data": body})


@dataclass
class MockUser:
    id: int = 1
    username: str = "admin"
    email: str = "admin@test.com"
    is_authenticated: bool = True
    is_active: bool = True
    active: int = 1
    roles: list = field(default_factory=list)


@dataclass
class MockRole:
    id: int = 1
    name: str = "Admin"


def _make_session_cookie(user_id: int) -> str:
    # Flask's exact signer configuration (hmac key-derivation + SHA-1) —
    # the decoder no longer accepts itsdangerous' django-concat default.
    s = URLSafeTimedSerializer(
        SECRET_KEY,
        salt="cookie-session",
        signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1},
    )
    return s.dumps({"_user_id": str(user_id)})


@get("/protected")
async def protected_route(request: Request) -> dict:
    user = request.user
    # The anonymous user (UnauthenticatedUser) has ``username == ""`` and
    # ``is_authenticated == False``; report it as "anon" for the assertions.
    if not getattr(user, "is_authenticated", False):
        return {"username": "anon"}
    return {"username": getattr(user, "username", "anon")}


@get("/public", opt={"exclude_from_auth": True})
async def public_route() -> dict:
    return {"msg": "public"}


def _make_settings(**overrides):
    defaults = {
        "secret_key": MagicMock(get_secret_value=MagicMock(return_value=SECRET_KEY)),
        "session_cookie_name": "session",
        "session_max_age": 86400,
        "embedded_superset": False,
        "guest_token_jwt_secret": "",
        "guest_token_jwt_algo": "HS256",
        # Real types required by the guest-token branch in
        # SupersetAuthMiddleware (a bare MagicMock would make
        # feature_flags.get() truthy and header_name.lower() fail).
        "feature_flags": {},
        "guest_token_header_name": "X-GuestToken",
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


@pytest.fixture
def mock_session_factory():
    session = AsyncMock()
    factory = MagicMock(return_value=session)
    factory.__aenter__ = AsyncMock(return_value=session)
    factory.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.fixture
def app(mock_session_factory):
    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": None,
        }
    )
    return Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )


async def test_unauthenticated_returns_anonymous_user(app):
    """Unauthenticated requests return anonymous user — RBAC guards handle denial."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/protected")
        assert resp.status_code == 200
        assert resp.json()["username"] == "anon"


async def test_public_route_no_auth_needed(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/public")
        assert resp.status_code == 200
        assert resp.json() == {"msg": "public"}


async def test_cookie_auth_success(app, mock_session_factory):
    mock_user = MockUser(roles=[MockRole()])

    with patch(
        "superset.middleware.auth.SupersetAuthMiddleware._resolve_user_from_db"
    ) as mock_resolve:
        mock_resolve.return_value = mock_user
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(1)
            resp = await client.get(
                "/protected",
                cookies={"session": cookie},
            )
            assert resp.status_code == 200
            assert resp.json()["username"] == "admin"


async def test_invalid_cookie_returns_anonymous(app):
    """Invalid cookies result in anonymous user (RBAC guards handle denial)."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(
            "/protected",
            cookies={"session": "invalid-cookie-data"},
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "anon"


async def test_jwt_auth_requires_embedded_superset_flag(mock_session_factory):
    """JWT auth should be rejected when embedded_superset is disabled."""
    state = State(
        {
            "settings": _make_settings(embedded_superset=False),
            "session_factory": mock_session_factory,
            "redis": None,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(
            "/protected",
            headers={"Authorization": "Bearer some.jwt.token"},
        )
        # JWT rejected → falls through to anonymous user
        assert resp.status_code == 200
        assert resp.json()["username"] == "anon"


async def test_jwt_auth_success_with_embedded_flag(mock_session_factory):
    """JWT auth should work when embedded_superset is enabled."""
    mock_guest_user = MockUser(id=0, username="guest")
    state = State(
        {
            "settings": _make_settings(embedded_superset=True),
            "session_factory": mock_session_factory,
            "redis": None,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    with patch(
        "superset.middleware.auth.SupersetAuthMiddleware._resolve_guest_from_jwt"
    ) as mock_resolve:
        mock_resolve.return_value = mock_guest_user
        async with AsyncTestClient(app=app) as client:
            resp = await client.get(
                "/protected",
                headers={"Authorization": "Bearer some.jwt.token"},
            )
            assert resp.status_code == 200


async def test_jwt_no_bearer_prefix_ignored(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(
            "/protected",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        # Non-Bearer auth → falls through to anonymous user
        assert resp.status_code == 200
        assert resp.json()["username"] == "anon"


# --- CachedUser tests ---


def test_cached_user_from_dict_with_roles():
    data = {
        "id": 1,
        "username": "admin",
        "email": "admin@test.com",
        "active": 1,
        "roles": [{"id": 1, "name": "Admin"}, {"id": 2, "name": "Gamma"}],
    }
    user = CachedUser.from_dict(data)
    assert user is not None
    assert user.id == 1
    assert user.username == "admin"
    assert len(user.roles) == 2
    assert user.roles[0].name == "Admin"
    assert user.active == 1


def test_cached_user_from_dict_inactive():
    data = {"id": 3, "username": "inactive", "active": 0, "roles": []}
    user = CachedUser.from_dict(data)
    assert user is not None
    assert user.active == 0


def test_cached_user_from_dict_invalid():
    assert CachedUser.from_dict({}) is None
    assert CachedUser.from_dict({"id": 1}) is None


async def test_redis_cache_rejects_inactive_user(mock_session_factory):
    """Deactivated users in Redis cache should be rejected."""
    mock_redis = AsyncMock()
    cached_data = _signed_cache_entry(
        {
            "id": 3,
            "username": "inactive",
            "email": "x@x.com",
            "active": 0,
            "roles": [],
        }
    )
    # ``_get_cached_user`` reads the entry and the cache epoch in one MGET.
    mock_redis.mget = AsyncMock(return_value=[cached_data, None])
    # Legacy-cookie blacklist check: no entry present -> None.
    mock_redis.get = AsyncMock(return_value=None)

    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": mock_redis,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    # Also mock DB fallback to return None (user inactive)
    with patch.object(
        SupersetAuthMiddleware,
        "_resolve_user_from_db",
        return_value=None,
    ):
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(3)
            resp = await client.get("/protected", cookies={"session": cookie})
            # Inactive user → falls through to anonymous user
            assert resp.status_code == 200
            assert resp.json()["username"] == "anon"


async def test_redis_cache_preserves_roles(mock_session_factory):
    """Cached users should retain their roles for permission checks."""
    mock_redis = AsyncMock()
    cached_data = _signed_cache_entry(
        {
            "id": 1,
            "username": "admin",
            "email": "admin@test.com",
            "active": 1,
            "roles": [{"id": 1, "name": "Admin"}],
        }
    )
    mock_redis.mget = AsyncMock(return_value=[cached_data, None])
    # The itsdangerous test cookie exercises the legacy-cookie blacklist
    # check (no per-token "iat"), which does a plain ``redis.get`` --
    # no entry present, so it must not be treated as blacklisted.
    mock_redis.get = AsyncMock(return_value=None)

    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": mock_redis,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    async with AsyncTestClient(app=app) as client:
        cookie = _make_session_cookie(1)
        resp = await client.get("/protected", cookies={"session": cookie})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"


async def test_redis_cache_rejected_when_epoch_moved(mock_session_factory):
    """A role/permission change bumps the epoch, retiring cached payloads.

    The cached entry still claims the Admin role, but it was minted under
    epoch 1 while Redis is now at 2, so the middleware must discard it and
    resolve the user from the database instead.
    """
    mock_redis = AsyncMock()
    stale = _signed_cache_entry(
        {
            "epoch": "1",
            "id": 1,
            "username": "stale-admin",
            "email": "admin@test.com",
            "active": 1,
            "roles": [{"id": 1, "name": "Admin"}],
        }
    )
    mock_redis.mget = AsyncMock(return_value=[stale, "2"])

    # ``redis.get`` is used for two unrelated things here: the legacy-cookie
    # blacklist check (no entry -> None) and, after the stale cache entry is
    # discarded and the user re-resolved from the DB, ``read_auth_epoch``
    # writing the fresh cache entry (-> "2").  Discriminate by key so
    # neither read spuriously interferes with the other.
    from superset.security.auth_cache import AUTH_EPOCH_KEY

    async def _fake_get(key: str) -> str | None:
        return "2" if key == AUTH_EPOCH_KEY else None

    mock_redis.get = AsyncMock(side_effect=_fake_get)

    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": mock_redis,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    with patch.object(
        SupersetAuthMiddleware,
        "_resolve_user_from_db",
        return_value=MockUser(username="fresh-admin"),
    ):
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(1)
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            assert resp.json()["username"] == "fresh-admin"


async def test_redis_cache_accepted_when_epoch_matches(mock_session_factory):
    """An entry minted under the current epoch is still served from cache."""
    mock_redis = AsyncMock()
    fresh = _signed_cache_entry(
        {
            "epoch": "2",
            "id": 1,
            "username": "cached-admin",
            "email": "admin@test.com",
            "active": 1,
            "roles": [{"id": 1, "name": "Admin"}],
        }
    )
    mock_redis.mget = AsyncMock(return_value=[fresh, "2"])
    # Legacy-cookie blacklist check: no entry present -> None.
    mock_redis.get = AsyncMock(return_value=None)

    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": mock_redis,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    async with AsyncTestClient(app=app) as client:
        cookie = _make_session_cookie(1)
        resp = await client.get("/protected", cookies={"session": cookie})
        assert resp.status_code == 200
        assert resp.json()["username"] == "cached-admin"


async def test_unsigned_redis_entry_is_rejected(mock_session_factory):
    """An entry written to Redis without a valid HMAC must not be trusted.

    The cached payload carries the user's roles and flattened permission set,
    so accepting it unsigned would make Redis write access equivalent to
    granting arbitrary permissions.
    """
    mock_redis = AsyncMock()
    forged = json.dumps(
        {
            "id": 1,
            "username": "attacker",
            "email": "a@b.c",
            "active": 1,
            "roles": [{"id": 1, "name": "Admin"}],
            "permissions": [["can_write", "Database"]],
        }
    )
    mock_redis.mget = AsyncMock(return_value=[forged, None])
    # Legacy-cookie blacklist check: no entry present -> None.
    mock_redis.get = AsyncMock(return_value=None)

    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": mock_redis,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    with patch.object(
        SupersetAuthMiddleware,
        "_resolve_user_from_db",
        return_value=None,
    ):
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(1)
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            assert resp.json()["username"] == "anon"


# ---------------------------------------------------------------------------
# H2 regression: a signed cache entry is bound to the Redis key it was
# written under, and to the user id its payload claims to be.
# ---------------------------------------------------------------------------


async def test_transplanted_redis_entry_is_rejected(mock_session_factory):
    """A legitimately-signed entry copied onto a different user's cache key
    (``COPY auth:user:1 auth:user:42`` with Redis write access, no
    signature forgery needed) must not verify under its new key."""
    mock_redis = AsyncMock()
    # Admin's real entry, correctly signed for its own key "auth:user:1"...
    admins_entry = _signed_cache_entry(
        {
            "id": 1,
            "username": "admin",
            "email": "admin@test.com",
            "active": 1,
            "roles": [{"id": 1, "name": "Admin"}],
            "permissions": [["can_write", "Database"]],
        },
        cache_key="auth:user:1",
    )
    # ...but the request authenticates as user 42, so the middleware reads
    # (and verifies) it under "auth:user:42" -- as if it had been COPY'd.
    mock_redis.mget = AsyncMock(return_value=[admins_entry, None])
    # Legacy-cookie blacklist check: no entry present -> None.
    mock_redis.get = AsyncMock(return_value=None)

    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": mock_redis,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    with patch.object(
        SupersetAuthMiddleware,
        "_resolve_user_from_db",
        return_value=None,
    ):
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(42)
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            # Must NOT come back as "admin" with Admin's cached permissions.
            assert resp.json()["username"] == "anon"


async def test_redis_entry_with_mismatched_payload_id_is_rejected(
    mock_session_factory,
):
    """Even an entry correctly signed *for its actual key* is rejected if
    the payload inside claims a different user id -- the id check is
    independent of, and in addition to, the key-bound signature."""
    mock_redis = AsyncMock()
    # Signed correctly for key "auth:user:42" (so the signature alone would
    # pass), but the payload content claims to be user 1 (admin).
    mismatched_entry = _signed_cache_entry(
        {
            "id": 1,
            "username": "admin",
            "email": "admin@test.com",
            "active": 1,
            "roles": [{"id": 1, "name": "Admin"}],
        },
        cache_key="auth:user:42",
    )
    mock_redis.mget = AsyncMock(return_value=[mismatched_entry, None])
    # Legacy-cookie blacklist check: no entry present -> None.
    mock_redis.get = AsyncMock(return_value=None)

    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": mock_redis,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    with patch.object(
        SupersetAuthMiddleware,
        "_resolve_user_from_db",
        return_value=None,
    ):
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(42)
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            assert resp.json()["username"] == "anon"


async def test_entry_signed_for_a_different_key_is_rejected_even_with_matching_id(
    mock_session_factory,
):
    """An entry whose payload id matches the user being looked up, but whose
    signature was minted for a *different* Redis key, must not verify.

    This isolates the key-bound signature check from the independent
    payload-id check above: the id here agrees with the lookup (so that
    check alone would let it through), and only the signature having been
    computed over the wrong key can reject it.
    """
    mock_redis = AsyncMock()
    # Payload id matches user 42 -- the target of this lookup -- but the
    # signature was computed for key "auth:user:1", not "auth:user:42".
    entry = _signed_cache_entry(
        {
            "id": 42,
            "username": "admin",
            "email": "admin@test.com",
            "active": 1,
            "roles": [{"id": 1, "name": "Admin"}],
        },
        cache_key="auth:user:1",
    )
    mock_redis.mget = AsyncMock(return_value=[entry, None])
    # Legacy-cookie blacklist check: no entry present -> None.
    mock_redis.get = AsyncMock(return_value=None)

    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": mock_redis,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    with patch.object(
        SupersetAuthMiddleware,
        "_resolve_user_from_db",
        return_value=None,
    ):
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(42)
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            assert resp.json()["username"] == "anon"


async def test_tampered_redis_entry_is_rejected(mock_session_factory):
    """Editing a signed payload invalidates its signature."""
    mock_redis = AsyncMock()
    signed = json.loads(
        _signed_cache_entry(
            {
                "id": 1,
                "username": "admin",
                "email": "admin@test.com",
                "active": 1,
                "roles": [{"id": 1, "name": "Gamma"}],
            }
        )
    )
    tampered = json.loads(signed["data"])
    tampered["roles"] = [{"id": 1, "name": "Admin"}]
    signed["data"] = json.dumps(tampered)
    mock_redis.mget = AsyncMock(return_value=[json.dumps(signed), None])
    # Legacy-cookie blacklist check: no entry present -> None.
    mock_redis.get = AsyncMock(return_value=None)

    state = State(
        {
            "settings": _make_settings(),
            "session_factory": mock_session_factory,
            "redis": mock_redis,
        }
    )
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[SupersetAuthMiddleware],
        state=state,
    )

    with patch.object(
        SupersetAuthMiddleware,
        "_resolve_user_from_db",
        return_value=None,
    ):
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(1)
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            assert resp.json()["username"] == "anon"


# ---------------------------------------------------------------------------
# H3 regression: the Public-role permission cache is signed + epoch-bound,
# exactly like the per-user cache, so Redis write access alone cannot grant
# anonymous callers permissions, and a role/permission mutation invalidates
# it immediately instead of after the full TTL.
# ---------------------------------------------------------------------------


async def test_public_role_cache_forged_unsigned_blob_is_rejected():
    """A bare, unsigned permission list -- the pre-fix format -- is rejected
    before signature verification even runs (not a ``{"sig", "data"}``
    envelope at all)."""
    mock_redis = AsyncMock()
    forged = json.dumps([["can_write", "Database"], ["can_read", "Dashboard"]])
    mock_redis.mget = AsyncMock(return_value=[forged, None])

    result = await SupersetAuthMiddleware._get_cached_public_role_perms(
        mock_redis, "Public", SECRET_KEY
    )
    assert result is None


async def test_public_role_cache_forged_entry_with_invalid_signature_is_rejected():
    """A well-shaped ``{"sig", "data"}`` envelope with a garbage signature
    -- exactly what Redis write access alone could plant, without the
    secret needed to mint a real signature -- must not grant anonymous
    callers permissions. Mirrors ``test_tampered_redis_entry_is_rejected``
    for the per-user cache, exercising the actual signature check instead
    of the earlier "is this even an envelope" shape check."""
    mock_redis = AsyncMock()
    payload = json.dumps(
        {
            "epoch": "",
            "role": "Public",
            "permissions": [["can_write", "Database"], ["can_read", "Dashboard"]],
        }
    )
    forged = json.dumps({"sig": "not-a-real-signature", "data": payload})
    mock_redis.mget = AsyncMock(return_value=[forged, None])

    result = await SupersetAuthMiddleware._get_cached_public_role_perms(
        mock_redis, "Public", SECRET_KEY
    )
    assert result is None


async def test_public_role_cache_signed_entry_is_accepted():
    """A correctly signed, current-epoch entry for the requested role is
    served from cache."""
    from superset.middleware.auth import _PUBLIC_ROLE_CACHE_KEY
    from superset.security.auth_cache import sign_keyed_payload

    mock_redis = AsyncMock()
    payload = json.dumps(
        {"epoch": "3", "role": "Public", "permissions": [["can_read", "Dashboard"]]}
    )
    envelope = json.dumps(
        {
            "sig": sign_keyed_payload(_PUBLIC_ROLE_CACHE_KEY, payload, SECRET_KEY),
            "data": payload,
        }
    )
    mock_redis.mget = AsyncMock(return_value=[envelope, "3"])

    result = await SupersetAuthMiddleware._get_cached_public_role_perms(
        mock_redis, "Public", SECRET_KEY
    )
    assert result == {("can_read", "Dashboard")}


async def test_public_role_cache_rejects_stale_epoch():
    """A revoked Public-role permission must not stay effective for the
    rest of the TTL: an entry minted under an old epoch is discarded."""
    from superset.middleware.auth import _PUBLIC_ROLE_CACHE_KEY
    from superset.security.auth_cache import sign_keyed_payload

    mock_redis = AsyncMock()
    payload = json.dumps(
        {"epoch": "1", "role": "Public", "permissions": [["can_read", "Dashboard"]]}
    )
    envelope = json.dumps(
        {
            "sig": sign_keyed_payload(_PUBLIC_ROLE_CACHE_KEY, payload, SECRET_KEY),
            "data": payload,
        }
    )
    # Redis is now at epoch 2 -- a permission_view/role mutation happened.
    mock_redis.mget = AsyncMock(return_value=[envelope, "2"])

    result = await SupersetAuthMiddleware._get_cached_public_role_perms(
        mock_redis, "Public", SECRET_KEY
    )
    assert result is None


async def test_public_role_cache_rejects_role_name_mismatch():
    """An entry signed for one role name must not be honoured for another
    -- renaming AUTH_ROLE_PUBLIC must not resurrect a stale grant."""
    from superset.middleware.auth import _PUBLIC_ROLE_CACHE_KEY
    from superset.security.auth_cache import sign_keyed_payload

    mock_redis = AsyncMock()
    payload = json.dumps(
        {
            "epoch": "1",
            "role": "OldPublicRole",
            "permissions": [["can_read", "Dashboard"]],
        }
    )
    envelope = json.dumps(
        {
            "sig": sign_keyed_payload(_PUBLIC_ROLE_CACHE_KEY, payload, SECRET_KEY),
            "data": payload,
        }
    )
    mock_redis.mget = AsyncMock(return_value=[envelope, "1"])

    result = await SupersetAuthMiddleware._get_cached_public_role_perms(
        mock_redis, "Public", SECRET_KEY
    )
    assert result is None


# ---------------------------------------------------------------------------
# H4 regression: a session cookie must carry a mandatory ``type: "session"``
# claim (plus a present ``exp``), so an unrelated HS256/SECRET_KEY JWT --
# notably the database-OAuth2 ``state`` JWT, which also carries a
# ``user_id`` claim and is placed in a query parameter sent to a
# third-party IdP -- cannot double as a valid session cookie.
#
# Policy: hard cut, no grace period.  A grace period that accepted
# type-less JWTs for a bounded window would also keep accepting the OAuth2
# ``state`` JWT during that same window (it is minted by
# superset.utils.oauth2, out of scope for this fix, and will never gain a
# "type" claim) -- so a grace period would not actually close this gap, it
# would just delay closing it. See the accompanying report for the full
# reasoning.
# ---------------------------------------------------------------------------


def _make_jwt_cookie(payload: dict, secret: str = SECRET_KEY) -> str:
    return jwt.encode(payload, secret, algorithm="HS256")


async def test_jwt_cookie_with_session_type_is_accepted(app):
    """A cookie minted by ``_create_session_cookie`` (type="session") is a
    valid session."""
    cookie = _make_jwt_cookie(
        {
            "type": "session",
            "user_id": 1,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
    )
    mock_user = MockUser(roles=[MockRole()])
    with patch(
        "superset.middleware.auth.SupersetAuthMiddleware._resolve_user_from_db"
    ) as mock_resolve:
        mock_resolve.return_value = mock_user
        async with AsyncTestClient(app=app) as client:
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            assert resp.json()["username"] == "admin"


async def test_jwt_cookie_without_type_claim_is_rejected(app):
    """A type-less HS256/SECRET_KEY JWT carrying ``user_id`` + ``exp`` --
    exactly the shape of the database-OAuth2 ``state`` JWT
    (``superset.utils.oauth2.encode_oauth2_state``) -- must NOT
    authenticate as a session, even though it decodes and verifies fine.

    ``_resolve_user_from_db`` is primed to return a real user -- as the
    accepted-cookie sibling test does -- so that if the gate were bypassed
    the request would come back authenticated as "admin" instead of
    incidentally falling through to anonymous because the DB lookup was
    never wired up.
    """
    cookie = _make_jwt_cookie(
        {
            "user_id": 1,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
    )
    mock_user = MockUser(roles=[MockRole()])
    with patch(
        "superset.middleware.auth.SupersetAuthMiddleware._resolve_user_from_db"
    ) as mock_resolve:
        mock_resolve.return_value = mock_user
        async with AsyncTestClient(app=app) as client:
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            assert resp.json()["username"] == "anon"


async def test_jwt_cookie_without_exp_is_rejected(app):
    """A ``type="session"`` JWT missing ``exp`` entirely is rejected --
    ``exp`` is mandatory, not just checked-if-present.

    ``_resolve_user_from_db`` is primed to return a real user so a gate
    bypass would surface as an authenticated "admin" response rather than
    an incidental anonymous fallback.
    """
    cookie = _make_jwt_cookie(
        {
            "type": "session",
            "user_id": 1,
            "iat": int(time.time()),
        }
    )
    mock_user = MockUser(roles=[MockRole()])
    with patch(
        "superset.middleware.auth.SupersetAuthMiddleware._resolve_user_from_db"
    ) as mock_resolve:
        mock_resolve.return_value = mock_user
        async with AsyncTestClient(app=app) as client:
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            assert resp.json()["username"] == "anon"


async def test_jwt_cookie_with_wrong_type_claim_is_rejected(app):
    """A JWT with an unrelated ``type`` value (e.g. an access/refresh token
    accidentally presented as a cookie) is rejected.

    ``_resolve_user_from_db`` is primed to return a real user so a gate
    bypass would surface as an authenticated "admin" response rather than
    an incidental anonymous fallback.
    """
    cookie = _make_jwt_cookie(
        {
            "type": "access",
            "user_id": 1,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
    )
    mock_user = MockUser(roles=[MockRole()])
    with patch(
        "superset.middleware.auth.SupersetAuthMiddleware._resolve_user_from_db"
    ) as mock_resolve:
        mock_resolve.return_value = mock_user
        async with AsyncTestClient(app=app) as client:
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 200
            assert resp.json()["username"] == "anon"


# ---------------------------------------------------------------------------
# Token / user blacklist: revocation semantics + fail-open visibility
# ---------------------------------------------------------------------------


class _RaisingRedis:
    """A Redis stand-in whose ``get`` always fails, to exercise the
    availability (fail-open) branch of the blacklist checks."""

    async def get(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("redis unavailable")


class _MapRedis:
    """Minimal async Redis stand-in backed by an in-memory dict."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    async def get(self, key: str) -> str | None:
        return self._m.get(key)


@pytest.mark.parametrize(
    "blacklist_ts, token_iat, expected",
    [
        (None, 1000, False),  # no logout recorded -> token accepted
        (1000, 999, True),  # issued before the logout instant -> revoked
        (1000, 1000, True),  # issued *at* the logout instant -> revoked
        (1000, 1001, False),  # issued after the logout instant -> accepted
        (1000, 100000, False),  # much later -> accepted
    ],
)
async def test_is_token_blacklisted_semantics(
    blacklist_ts: int | None, token_iat: int, expected: bool
) -> None:
    """A token is revoked iff it was issued at or before the recorded logout
    timestamp. Pins the ``token_iat <= blacklist_ts`` comparison so a flip to
    ``<`` (which would let a token minted in the same second survive logout) is
    caught."""
    mapping = (
        {"auth:token_blacklist:7": str(blacklist_ts)} if blacklist_ts is not None else {}
    )
    result = await SupersetAuthMiddleware._is_token_blacklisted(
        _MapRedis(mapping), 7, token_iat
    )
    assert result is expected


async def test_is_token_blacklisted_fails_open_and_warns(caplog) -> None:
    """A Redis outage must not block authentication (fail-open), but the
    skipped revocation check must be visible at WARNING, not swallowed at
    DEBUG."""
    with caplog.at_level(logging.WARNING, logger="superset.middleware.auth"):
        result = await SupersetAuthMiddleware._is_token_blacklisted(
            _RaisingRedis(), 7, 1000
        )
    assert result is False
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


@pytest.mark.parametrize(
    "mapping, expected",
    [
        ({"auth:token_blacklist:7": "1"}, True),
        ({}, False),
    ],
)
async def test_is_user_blacklisted_presence(
    mapping: dict[str, str], expected: bool
) -> None:
    """The legacy-cookie check revokes on the mere presence of a blacklist
    entry (no per-token timestamp to compare)."""
    result = await SupersetAuthMiddleware._is_user_blacklisted(_MapRedis(mapping), 7)
    assert result is expected


async def test_is_user_blacklisted_fails_open_and_warns(caplog) -> None:
    """Legacy-cookie blacklist check: fail-open on Redis error, but warn."""
    with caplog.at_level(logging.WARNING, logger="superset.middleware.auth"):
        result = await SupersetAuthMiddleware._is_user_blacklisted(_RaisingRedis(), 7)
    assert result is False
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)

