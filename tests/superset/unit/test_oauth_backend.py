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
"""Tests for OAuthAuthBackend state/CSRF handling.

The OAuth state token is the CSRF pivot of the login flow: it binds the
callback to a request this server initiated. These tests exercise the real
``sign_state`` / ``verify_state`` / ``handle_callback`` state checks (which
run before any network call), not a mocked backend.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt as _pyjwt
import pytest

from superset.security.auth.oauth import OAuthAuthBackend, OAuthCallbackError


def _backend() -> OAuthAuthBackend:
    settings = SimpleNamespace(
        secret_key="test-secret-key-at-least-32-bytes-long-xx",
        oauth_providers=[{"name": "keycloak", "remote_app": {"client_id": "rp"}}],
    )
    return OAuthAuthBackend(MagicMock(), settings=settings)


def test_sign_verify_state_roundtrip():
    backend = _backend()
    token = backend.sign_state({"provider": "keycloak", "next": "/dashboard/1/"})
    payload = backend.verify_state(token)
    assert payload["provider"] == "keycloak"
    assert payload["next"] == "/dashboard/1/"


def test_verify_state_rejects_tampered_token():
    backend = _backend()
    with pytest.raises(OAuthCallbackError):
        backend.verify_state("not.a.valid.jwt")


def test_verify_state_rejects_foreign_signature():
    """A state signed with a different key must not verify."""
    other = SimpleNamespace(secret_key="a-different-secret-key-of-sufficient-len")
    foreign = OAuthAuthBackend(MagicMock(), settings=other).sign_state(
        {"provider": "keycloak"}
    )
    with pytest.raises(OAuthCallbackError):
        _backend().verify_state(foreign)


async def test_handle_callback_rejects_state_cookie_mismatch():
    """CSRF: returned state must equal the state stored in the cookie."""
    backend = _backend()
    signed = backend.sign_state({"provider": "keycloak"})
    with pytest.raises(OAuthCallbackError, match="state mismatch"):
        await backend.handle_callback(
            "keycloak",
            code="c",
            state=signed,
            signed_state_cookie="something-else",
            redirect_uri="https://app/oauth-authorized/keycloak",
        )


async def test_handle_callback_rejects_empty_state():
    backend = _backend()
    with pytest.raises(OAuthCallbackError):
        await backend.handle_callback(
            "keycloak",
            code="c",
            state="",
            signed_state_cookie="",
            redirect_uri="https://app/oauth-authorized/keycloak",
        )


async def test_handle_callback_rejects_provider_mismatch():
    """State minted for one provider must not be replayed at another's callback."""
    backend = _backend()
    signed = backend.sign_state({"provider": "other"})
    with pytest.raises(OAuthCallbackError, match="provider mismatch"):
        await backend.handle_callback(
            "keycloak",
            code="c",
            state=signed,
            signed_state_cookie=signed,
            redirect_uri="https://app/oauth-authorized/keycloak",
        )


# ---------------------------------------------------------------------------
# get_user_info — per-provider identity mapping (openshift / authentik / azure)
# ---------------------------------------------------------------------------

_ENDPOINTS = {
    "userinfo_url": "",
    "jwks_uri": "",
    "issuer": "",
    "token_url": "",
    "authorize_url": "",
}


def _unsigned_jwt(claims: dict) -> str:
    return _pyjwt.encode(claims, "irrelevant-secret", algorithm="HS256")


async def test_get_user_info_openshift_uses_cluster_user_api():
    """OpenShift has no OIDC userinfo; identity comes from the cluster's
    custom user API (GET apis/user.openshift.io/v1/users/~)."""
    backend = _backend()
    backend._http_get_json = AsyncMock(return_value={"metadata": {"name": "alice"}})
    info = await backend.get_user_info(
        provider_name="openshift",
        provider={"name": "openshift", "api_base_url": "https://oc.example.com/"},
        token_resp={"access_token": "tok"},
        endpoints=_ENDPOINTS,
    )
    assert info == {"username": "openshift_alice"}
    called_url = backend._http_get_json.call_args.args[0]
    assert called_url == "https://oc.example.com/apis/user.openshift.io/v1/users/~"


async def test_get_user_info_authentik_maps_nickname_and_preferred_username():
    """Authentik's idiosyncratic mapping: username<-nickname, email<-
    preferred_username (distinct from the generic OIDC claim names)."""
    backend = _backend()
    id_token = _unsigned_jwt(
        {
            "preferred_username": "alice@corp.com",
            "nickname": "alice",
            "given_name": "Alice",
            "family_name": "Doe",
            "groups": ["admins"],
        }
    )
    info = await backend.get_user_info(
        provider_name="authentik",
        provider={"name": "authentik"},
        token_resp={"id_token": id_token},
        endpoints=_ENDPOINTS,
    )
    assert info["username"] == "alice"
    assert info["email"] == "alice@corp.com"
    assert info["role_keys"] == ["admins"]


async def test_get_user_info_azure_unsafe_decode_is_default():
    """Azure default (verify_signature unset) decodes the id_token without
    network validation — 1:1 with FAB's default branch."""
    backend = _backend()
    id_token = _unsigned_jwt(
        {"oid": "az-oid", "given_name": "Bob", "upn": "bob@corp.com", "roles": ["r1"]}
    )
    info = await backend.get_user_info(
        provider_name="azure",
        provider={"name": "azure"},
        token_resp={"id_token": id_token},
        endpoints=_ENDPOINTS,
    )
    assert info["username"] == "az-oid"
    assert info["email"] == "bob@corp.com"
    assert info["role_keys"] == ["r1"]


async def test_get_user_info_azure_verify_signature_validates(monkeypatch):
    """When verify_signature is set, Azure id_token must be validated against
    Microsoft's JWKS — a failure rejects the login (no silent unsafe decode)."""
    backend = _backend()
    id_token = _unsigned_jwt({"oid": "az-oid"})

    async def _boom(tok):
        raise OAuthCallbackError("Azure id_token validation failed: bad signature")

    backend._validate_azure_jwt = _boom
    with pytest.raises(OAuthCallbackError, match="Azure id_token validation failed"):
        await backend.get_user_info(
            provider_name="azure",
            provider={"name": "azure", "client_kwargs": {"verify_signature": True}},
            token_resp={"id_token": id_token},
            endpoints=_ENDPOINTS,
        )


# ---------------------------------------------------------------------------
# OIDC nonce is minted into the authorize URL and the signed state
# ---------------------------------------------------------------------------


def _backend_with_authorize_url() -> OAuthAuthBackend:
    settings = SimpleNamespace(
        secret_key="test-secret-key-at-least-32-bytes-long-xx",
        oauth_providers=[
            {
                "name": "keycloak",
                "remote_app": {
                    "client_id": "rp",
                    "authorize_url": "https://idp/authorize",
                },
            }
        ],
    )
    return OAuthAuthBackend(MagicMock(), settings=settings)


async def test_build_authorize_url_mints_nonce_into_params_and_state():
    """A fresh nonce is sent to the IdP and stored in the signed state so the
    callback can bind the returned id_token to this request."""
    from urllib.parse import parse_qs, urlparse

    backend = _backend_with_authorize_url()
    url, state = await backend.build_authorize_url(
        "keycloak", redirect_uri="https://app/oauth-authorized/keycloak"
    )
    qs = parse_qs(urlparse(url).query)
    assert qs["nonce"][0]
    payload = backend.verify_state(state)
    assert payload["nonce"] == qs["nonce"][0]


async def test_get_user_info_okta_missing_sub_does_not_raise():
    """A userinfo response missing ``sub`` must not raise a KeyError (500)."""
    backend = _backend()
    backend._http_get_json = AsyncMock(return_value={"email": "a@b.com"})
    info = await backend.get_user_info(
        provider_name="okta",
        provider={"name": "okta"},
        token_resp={"access_token": "tok"},
        endpoints={**_ENDPOINTS, "userinfo_url": "https://idp/userinfo"},
    )
    assert info["username"] == "okta_"
    assert info["email"] == "a@b.com"
