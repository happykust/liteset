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
from unittest.mock import MagicMock

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
