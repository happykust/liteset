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
"""Tests for :mod:`superset.security.auth.oidc`.

Covers OIDC ``id_token`` validation — in particular that the audience
(``aud``) claim is always enforced when a ``client_id`` is configured,
regardless of whether the provider uses the top-level or ``remote_app``
config layout.  An empty audience would make PyJWT set
``verify_aud=False`` and accept an ``id_token`` minted for a *different*
relying party.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest

from superset.security.auth.oauth import OAuthCallbackError
from superset.security.auth.oidc import OIDCAuthBackend


def _make_settings(secret_key: str = "x" * 32) -> Any:
    settings = MagicMock()
    settings.secret_key = secret_key
    settings.oauth_providers = []
    return settings


def _make_backend() -> OIDCAuthBackend:
    sm = MagicMock()
    return OIDCAuthBackend(sm, settings=_make_settings())


# ---------------------------------------------------------------------------
# Audience resolution: remote_app layout must still enforce aud
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_info_enforces_audience_from_remote_app_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``client_id`` under ``remote_app`` must be passed to ``validate_id_token``.

    Regression guard: previously ``audience`` was read from the *raw*
    provider dict (``provider.get('client_id')``), which is ``None`` when
    the provider uses the ``remote_app`` sub-key layout.  An empty
    audience disables ``verify_aud`` and accepts id_tokens minted for a
    different relying party.
    """
    backend = _make_backend()

    captured: dict[str, Any] = {}

    async def _fake_validate(
        *,
        id_token: str,
        jwks_uri: str,
        issuer: str = "",
        audience: str = "",
        nonce: str = "",
    ) -> dict[str, Any]:
        captured["audience"] = audience
        return {"preferred_username": "alice", "email": "alice@example.com"}

    monkeypatch.setattr(backend, "validate_id_token", _fake_validate)

    provider = {
        "name": "keycloak",
        "remote_app": {"client_id": "my-relying-party"},
    }
    endpoints = {
        "jwks_uri": "https://idp/jwks",
        "issuer": "https://idp/",
        "userinfo_url": "",
    }

    userinfo = await backend.get_user_info(
        provider_name="keycloak",
        provider=provider,
        token_resp={"id_token": "the.id.token"},
        endpoints=endpoints,
    )

    assert captured["audience"] == "my-relying-party"
    assert userinfo["username"] == "alice"


@pytest.mark.asyncio
async def test_get_user_info_enforces_audience_from_toplevel_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level ``client_id`` layout must also pass the audience through."""
    backend = _make_backend()

    captured: dict[str, Any] = {}

    async def _fake_validate(
        *,
        id_token: str,
        jwks_uri: str,
        issuer: str = "",
        audience: str = "",
        nonce: str = "",
    ) -> dict[str, Any]:
        captured["audience"] = audience
        return {"preferred_username": "bob"}

    monkeypatch.setattr(backend, "validate_id_token", _fake_validate)

    provider = {"name": "generic", "client_id": "top-level-rp"}
    endpoints = {"jwks_uri": "https://idp/jwks", "issuer": "", "userinfo_url": ""}

    await backend.get_user_info(
        provider_name="generic",
        provider=provider,
        token_resp={"id_token": "the.id.token"},
        endpoints=endpoints,
    )

    assert captured["audience"] == "top-level-rp"


# ---------------------------------------------------------------------------
# Signature/aud failure must NOT silently fall through to userinfo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_id_token_validation_failure_does_not_fall_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed id_token validation must propagate, not mask via userinfo.

    Previously :class:`OAuthCallbackError` raised from ``validate_id_token``
    (bad signature / wrong audience) was swallowed and the flow silently
    fell back to the UserInfo endpoint, hiding the rejection.
    """
    backend = _make_backend()

    async def _boom(
        *,
        id_token: str,
        jwks_uri: str,
        issuer: str = "",
        audience: str = "",
        nonce: str = "",
    ) -> dict[str, Any]:
        raise OAuthCallbackError("bad signature")

    monkeypatch.setattr(backend, "validate_id_token", _boom)

    # If the failure were swallowed, this userinfo would be returned.
    async def _fake_userinfo(url: str, *, bearer: str = "") -> dict[str, Any]:
        return {"preferred_username": "attacker"}

    monkeypatch.setattr(backend, "_http_get_json", _fake_userinfo)

    provider = {"name": "keycloak", "remote_app": {"client_id": "rp"}}
    endpoints = {
        "jwks_uri": "https://idp/jwks",
        "issuer": "https://idp/",
        "userinfo_url": "https://idp/userinfo",
    }

    with pytest.raises(OAuthCallbackError):
        await backend.get_user_info(
            provider_name="keycloak",
            provider=provider,
            token_resp={
                "id_token": "the.id.token",
                "access_token": "at",
            },
            endpoints=endpoints,
        )


# ---------------------------------------------------------------------------
# validate_id_token: wrong-audience id_token is rejected end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_id_token_rejects_wrong_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id_token whose ``aud`` differs from the configured client_id fails."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    id_token = pyjwt.encode(
        {"aud": "some-other-rp", "sub": "alice"},
        private_key,
        algorithm="RS256",
    )

    backend = _make_backend()

    class _SigningKey:
        key = public_key

    class _FakeJWKClient:
        def __init__(self, uri: str) -> None:
            pass

        def get_signing_key_from_jwt(self, token: str) -> Any:
            return _SigningKey()

    monkeypatch.setattr(pyjwt, "PyJWKClient", _FakeJWKClient)

    with pytest.raises(OAuthCallbackError):
        await backend.validate_id_token(
            id_token=id_token,
            jwks_uri="https://idp/jwks",
            audience="expected-rp",
        )


# ---------------------------------------------------------------------------
# validate_id_token: OIDC nonce replay-protection
# ---------------------------------------------------------------------------


def _signed_token(claims: dict[str, Any]):
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = pyjwt.encode(claims, private_key, algorithm="RS256")
    return token, private_key.public_key()


def _patch_jwks(monkeypatch, public_key) -> None:
    class _SigningKey:
        key = public_key

    class _FakeJWKClient:
        def __init__(self, uri: str) -> None:
            pass

        def get_signing_key_from_jwt(self, token: str) -> Any:
            return _SigningKey()

    monkeypatch.setattr(pyjwt, "PyJWKClient", _FakeJWKClient)


@pytest.mark.asyncio
async def test_validate_id_token_rejects_nonce_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id_token whose nonce differs from the authorization request fails."""
    id_token, public_key = _signed_token({"sub": "alice", "nonce": "minted-NONCE"})
    _patch_jwks(monkeypatch, public_key)
    backend = _make_backend()
    with pytest.raises(OAuthCallbackError):
        await backend.validate_id_token(
            id_token=id_token,
            jwks_uri="https://idp/jwks",
            nonce="expected-NONCE",
        )


@pytest.mark.asyncio
async def test_validate_id_token_accepts_matching_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching nonce passes validation and returns the claims."""
    id_token, public_key = _signed_token({"sub": "alice", "nonce": "shared-NONCE"})
    _patch_jwks(monkeypatch, public_key)
    backend = _make_backend()
    claims = await backend.validate_id_token(
        id_token=id_token,
        jwks_uri="https://idp/jwks",
        nonce="shared-NONCE",
    )
    assert claims["sub"] == "alice"
