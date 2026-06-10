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
"""Unit tests for the shared
:func:`superset.middleware.async_token.resolve_async_channel_id_from_request`
helper.

Coverage:
* valid cookie → returns channel claim
* missing cookie → returns None
* cookie signed with wrong secret → returns None
* settings with a ``SecretStr`` GAQ secret → secret is unwrapped via
  ``get_secret_value`` before JWT verification
* custom cookie name honoured
* settings is None → returns None
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import jwt as pyjwt

from superset.middleware.async_token import resolve_async_channel_id_from_request

GAQ_SECRET = "test-gaq-secret-at-least-16-chars"
COOKIE_NAME = "async-token"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _mint_token(channel: str, secret: str = GAQ_SECRET) -> str:
    """Mint a JWT in the exact shape AsyncTokenMiddleware produces."""
    token = pyjwt.encode(
        {"channel": channel, "sub": "1"},
        secret,
        algorithm="HS256",
    )
    return token.decode("ascii") if isinstance(token, bytes) else token


def _make_request(cookie_header: str | None = None) -> MagicMock:
    """Build a minimal mock request with ``scope["headers"]``."""
    request = MagicMock()
    if cookie_header is not None:
        request.scope = {"headers": [(b"cookie", cookie_header.encode("utf-8"))]}
    else:
        request.scope = {"headers": []}
    return request


def _make_settings(
    secret: object = GAQ_SECRET,
    cookie_name: str = COOKIE_NAME,
) -> MagicMock:
    """Build a mock settings object."""
    settings = MagicMock()
    settings.global_async_queries_jwt_secret = secret
    settings.global_async_queries_jwt_cookie_name = cookie_name
    return settings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_cookie_returns_channel():
    """A well-formed cookie yields the ``channel`` claim."""
    channel = str(uuid.uuid4())
    token = _mint_token(channel)
    request = _make_request(f"{COOKIE_NAME}={token}")
    settings = _make_settings()

    result = resolve_async_channel_id_from_request(request, settings)

    assert result == channel


def test_missing_cookie_returns_none():
    """No cookie header → None (caller decides what to do)."""
    request = _make_request(None)  # empty headers
    settings = _make_settings()

    result = resolve_async_channel_id_from_request(request, settings)

    assert result is None


def test_wrong_secret_returns_none():
    """Cookie signed with a different key → PyJWTError swallowed → None."""
    channel = str(uuid.uuid4())
    token = _mint_token(channel, secret="some-other-wrong-secret-32-bytes-long")
    request = _make_request(f"{COOKIE_NAME}={token}")
    settings = _make_settings()  # settings uses GAQ_SECRET

    result = resolve_async_channel_id_from_request(request, settings)

    assert result is None


def test_secretstr_secret_is_unwrapped():
    """When the GAQ secret is a ``SecretStr``, ``get_secret_value`` is called."""
    from pydantic import SecretStr

    channel = str(uuid.uuid4())
    token = _mint_token(channel, secret=GAQ_SECRET)
    request = _make_request(f"{COOKIE_NAME}={token}")
    settings = _make_settings(secret=SecretStr(GAQ_SECRET))

    result = resolve_async_channel_id_from_request(request, settings)

    assert result == channel


def test_custom_cookie_name_honoured():
    """When the settings specify a non-default cookie name it must be used."""
    custom_name = "my-async-jwt"
    channel = str(uuid.uuid4())
    token = _mint_token(channel)
    # Present ONLY under the custom name; default "async-token" is absent.
    request = _make_request(f"{custom_name}={token}")
    settings = _make_settings(cookie_name=custom_name)

    result = resolve_async_channel_id_from_request(request, settings)

    assert result == channel


def test_custom_cookie_name_not_found_returns_none():
    """Cookie present under default name but settings expect a different name → None."""
    channel = str(uuid.uuid4())
    token = _mint_token(channel)
    request = _make_request(f"{COOKIE_NAME}={token}")  # default name
    settings = _make_settings(cookie_name="expected-other-name")

    result = resolve_async_channel_id_from_request(request, settings)

    assert result is None


def test_settings_none_returns_none():
    """None settings → None immediately, no exception."""
    channel = str(uuid.uuid4())
    token = _mint_token(channel)
    request = _make_request(f"{COOKIE_NAME}={token}")

    result = resolve_async_channel_id_from_request(request, None)

    assert result is None


def test_helper_is_exported():
    """``resolve_async_channel_id_from_request`` must appear in ``__all__``."""
    from superset.middleware import async_token

    assert "resolve_async_channel_id_from_request" in async_token.__all__
