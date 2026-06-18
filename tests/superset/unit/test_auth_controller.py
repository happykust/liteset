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
"""Tests for authentication controller -- password hashing, email login,
timing-safe behaviour, and no-cache header enforcement.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest
from litestar.response import Redirect

from superset.controllers.auth import (
    _check_password_hash,
    _FAKE_PASSWORD_HASH,
    AuthController,
)

# ``_hash_internal`` is a helper in ``superset.utils.password`` (the auth
# controller only re-exports ``check_password_hash`` / ``_FAKE_PASSWORD_HASH``).
from superset.utils.password import _hash_internal

# ---------------------------------------------------------------------------
# Pre-computed hashes (generated with werkzeug 3.1 for "correct-password")
# ---------------------------------------------------------------------------
SCRYPT_HASH = (
    "scrypt:32768:8:1$2soUhTMsQebEVjb5$"
    "dda76edaa3335a0d7829445337e32a6ebd827e756b4f9bb8c17df3149cd37043"
    "12f837ffb3d57cc195eba6735dda194ff69d44c18c381eb122510e7b63ff6870"
)

PBKDF2_HASH = (
    "pbkdf2:sha256:1000000$3R1D5OnEsd8uBscb$"
    "b61f24e529b2ad4058e80268725cd03eccc89e02d57b6ac2f659f9f366c61fd0"
)


# ===================================================================
# _hash_internal tests
# ===================================================================


class TestHashInternal:
    """Low-level hash computation via _hash_internal."""

    def test_scrypt_with_params(self) -> None:
        """scrypt:N:r:p produces the same hex as hashlib.scrypt."""
        password = "hello"
        salt = "somesalt"
        result = _hash_internal("scrypt:32768:8:1", salt, password)
        expected = hashlib.scrypt(
            password.encode(),
            salt=salt.encode(),
            n=32768,
            r=8,
            p=1,
            maxmem=132 * 32768 * 8 * 1,
        ).hex()
        assert result == expected

    def test_scrypt_defaults(self) -> None:
        """Bare 'scrypt' (no params) uses defaults 2**15, 8, 1."""
        password = "hello"
        salt = "somesalt"
        result = _hash_internal("scrypt", salt, password)
        expected = hashlib.scrypt(
            password.encode(),
            salt=salt.encode(),
            n=2**15,
            r=8,
            p=1,
            maxmem=132 * (2**15) * 8 * 1,
        ).hex()
        assert result == expected

    def test_pbkdf2_with_params(self) -> None:
        """pbkdf2:sha256:N produces the same hex as hashlib.pbkdf2_hmac."""
        password = "hello"
        salt = "somesalt"
        result = _hash_internal("pbkdf2:sha256:10000", salt, password)
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 10000
        ).hex()
        assert result == expected

    def test_pbkdf2_defaults(self) -> None:
        """Bare 'pbkdf2' uses sha256 and 600000 iterations."""
        password = "hello"
        salt = "somesalt"
        result = _hash_internal("pbkdf2", salt, password)
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 600_000
        ).hex()
        assert result == expected

    def test_unsupported_method_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported hash method"):
            _hash_internal("bcrypt", "salt", "pw")

    def test_invalid_scrypt_parts_raises(self) -> None:
        """scrypt with wrong number of colon-separated params raises."""
        with pytest.raises(ValueError, match="Invalid scrypt method"):
            _hash_internal("scrypt:32768:8", "salt", "pw")


# ===================================================================
# _check_password_hash tests
# ===================================================================


class TestCheckPasswordHash:
    """Full round-trip verification of werkzeug-format hashes."""

    def test_scrypt_correct_password(self) -> None:
        assert _check_password_hash(SCRYPT_HASH, "correct-password") is True

    def test_scrypt_wrong_password(self) -> None:
        assert _check_password_hash(SCRYPT_HASH, "wrong-password") is False

    def test_pbkdf2_correct_password(self) -> None:
        assert _check_password_hash(PBKDF2_HASH, "correct-password") is True

    def test_pbkdf2_wrong_password(self) -> None:
        assert _check_password_hash(PBKDF2_HASH, "wrong-password") is False

    def test_empty_hash_returns_false(self) -> None:
        assert _check_password_hash("", "password") is False

    def test_empty_password_returns_false(self) -> None:
        assert _check_password_hash(SCRYPT_HASH, "") is False

    def test_none_hash_returns_false(self) -> None:
        # intentionally passing the wrong type (suppressed on the assert below)
        assert _check_password_hash(None, "password") is False  # type: ignore[arg-type]

    def test_malformed_hash_returns_false(self) -> None:
        assert _check_password_hash("not-a-hash", "password") is False

    def test_missing_dollar_signs_returns_false(self) -> None:
        assert _check_password_hash("pbkdf2:sha256:10000", "password") is False

    def test_fake_password_hash_is_valid_format(self) -> None:
        """_FAKE_PASSWORD_HASH must parse without error (for timing balance)."""
        # Should not raise -- result doesn't matter, only that it computes
        result = _check_password_hash(_FAKE_PASSWORD_HASH, "password")
        # The fake hash is all zeros, so it should never match a real password
        assert result is False

    def test_timing_safe_comparison(self) -> None:
        """Verify that hmac.compare_digest is used (not ==)."""
        # We can't easily test timing, but we CAN verify the code path
        # works for both match and mismatch without short-circuiting
        assert _check_password_hash(SCRYPT_HASH, "correct-password") is True
        assert _check_password_hash(SCRYPT_HASH, "almost-correct-password") is False

    def test_hash_check_invokes_compare_digest(self, monkeypatch) -> None:
        """The final comparison MUST go through ``hmac.compare_digest``.

        Asserting behaviour (match/mismatch) cannot distinguish a
        constant-time compare from ``==``; spy on the primitive itself so a
        regression to ``computed == hash_value`` is caught.
        """
        import superset.utils.password as pw

        real = pw.hmac.compare_digest
        calls: list[tuple[str, str]] = []

        def _spy(a, b):
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(pw.hmac, "compare_digest", _spy)
        assert _check_password_hash(SCRYPT_HASH, "correct-password") is True
        assert _check_password_hash(SCRYPT_HASH, "wrong-password") is False
        assert len(calls) == 2, "hmac.compare_digest not used for both checks"


# ===================================================================
# Werkzeug interop (validates our implementation matches werkzeug)
# ===================================================================


class TestWerkzeugInterop:
    """Verify our _check_password_hash matches werkzeug's check_password_hash."""

    def test_scrypt_matches_werkzeug(self) -> None:
        """Hash generated by werkzeug verifies correctly with our code."""
        # This hash was generated by werkzeug 3.1.7
        assert _check_password_hash(SCRYPT_HASH, "correct-password") is True

    def test_pbkdf2_matches_werkzeug(self) -> None:
        """Hash generated by werkzeug verifies correctly with our code."""
        # This hash was generated by werkzeug 3.1.7
        assert _check_password_hash(PBKDF2_HASH, "correct-password") is True

    def test_generate_and_verify_scrypt(self) -> None:
        """Round-trip: generate with _hash_internal, verify with _check."""
        salt = "TestSalt12345678"
        method = "scrypt:32768:8:1"
        h = _hash_internal(method, salt, "my-password")
        stored = f"{method}${salt}${h}"
        assert _check_password_hash(stored, "my-password") is True
        assert _check_password_hash(stored, "not-my-password") is False

    def test_generate_and_verify_pbkdf2(self) -> None:
        """Round-trip: generate with _hash_internal, verify with _check."""
        salt = "TestSalt12345678"
        method = "pbkdf2:sha256:10000"
        h = _hash_internal(method, salt, "my-password")
        stored = f"{method}${salt}${h}"
        assert _check_password_hash(stored, "my-password") is True
        assert _check_password_hash(stored, "not-my-password") is False


# ===================================================================
# login_page no-cache header tests
# ===================================================================


def _get_raw_method(controller_cls: type, method_name: str):
    """Return the underlying async function from a Litestar-decorated handler."""
    handler = getattr(controller_cls, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


_login_page_fn = _get_raw_method(AuthController, "login_page")


class TestLoginPageNoCacheHeaders:
    """login_page must set no-cache headers on ALL response paths.

    Original: FAB @no_cache wraps SupersetAuthView.login via make_response(),
    so EVERY return value (early-return redirect for authenticated users AND
    the render_app_template() Template path) receives:
        Cache-Control: no-store, no-cache, must-revalidate, max-age=0
        Pragma: no-cache
        Expires: 0

    Regression: the early-return Redirect(path="/") at the authenticated-user
    branch was missing these headers while the Template branch already had them.
    """

    @pytest.mark.asyncio
    async def test_authenticated_redirect_carries_no_cache_headers(self) -> None:
        """Early-return Redirect for an already-authenticated user must include
        all three no-cache headers (Cache-Control, Pragma, Expires).
        """
        controller = MagicMock()

        user = MagicMock()
        user.is_authenticated = True

        request = MagicMock()
        request.user = user

        settings = MagicMock()
        settings.auth_role_public = ""
        state = MagicMock()
        state.settings = settings

        result = await _login_page_fn(controller, request=request, state=state)

        assert isinstance(result, Redirect)
        assert result.url == "/"
        assert (
            result.headers.get("Cache-Control")
            == "no-store, no-cache, must-revalidate, max-age=0"
        )
        assert result.headers.get("Pragma") == "no-cache"
        assert result.headers.get("Expires") == "0"

    @pytest.mark.asyncio
    async def test_authenticated_redirect_target_is_index(self) -> None:
        """The redirect must point to '/' — the index, matching
        appbuilder.get_url_for_index in the original.
        """
        controller = MagicMock()

        user = MagicMock()
        user.is_authenticated = True

        request = MagicMock()
        request.user = user

        settings = MagicMock()
        settings.auth_role_public = ""
        state = MagicMock()
        state.settings = settings

        result = await _login_page_fn(controller, request=request, state=state)

        assert isinstance(result, Redirect)
        assert result.url == "/"

    @pytest.mark.asyncio
    async def test_unauthenticated_user_gets_no_cache_on_template(self) -> None:
        """Template response for unauthenticated users also carries no-cache headers
        (this was already correct; test ensures the fix did not regress it).
        """
        from unittest.mock import patch

        from litestar.response import Template

        controller = MagicMock()

        # UnauthenticatedUser: user.is_authenticated is False
        user = MagicMock()
        user.is_authenticated = False

        request = MagicMock()
        request.user = user
        request.cookies.get = MagicMock(return_value=None)

        settings = MagicMock()
        settings.auth_role_public = ""
        settings.static_assets_prefix = ""
        state = MagicMock()
        state.settings = settings

        with patch(
            "superset.controllers.auth._build_bootstrap_data",
            return_value={"common": {}, "user": {}},
        ):
            result = await _login_page_fn(controller, request=request, state=state)

        assert isinstance(result, Template)
        assert (
            result.headers.get("Cache-Control")
            == "no-store, no-cache, must-revalidate, max-age=0"
        )
        assert result.headers.get("Pragma") == "no-cache"
        assert result.headers.get("Expires") == "0"


# ===================================================================
# OAuth / OIDC login wiring
# ===================================================================

_oauth_login_fn = _get_raw_method(AuthController, "oauth_login")
_oauth_authorized_fn = _get_raw_method(AuthController, "oauth_authorized")


def _oauth_settings() -> MagicMock:
    """A settings mock configured for AUTH_OAUTH with one Keycloak provider."""
    settings = MagicMock()
    settings.auth_type = 4  # AUTH_OAUTH
    settings.secret_key = "x" * 32
    settings.session_cookie_name = "session"
    settings.session_cookie_secure = False
    settings.session_cookie_httponly = True
    settings.session_cookie_samesite = "lax"
    settings.session_max_age = 3600
    settings.oauth_providers = [
        {
            "name": "keycloak",
            "remote_app": {
                "client_id": "liteset",
                "client_secret": "secret",
                "authorize_url": "https://idp/auth",
                "access_token_url": "https://idp/token",
                "userinfo_url": "https://idp/userinfo",
            },
        }
    ]
    return settings


class _FakeAsyncSession:
    """Minimal async-context-manager session for callback tests."""

    def __init__(self) -> None:
        self.committed = False

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


_login_submit_fn = _get_raw_method(AuthController, "login_submit")


class TestBrowserLdapLogin:
    """POST /login/ dispatches to LDAP when AUTH_TYPE == AUTH_LDAP."""

    @pytest.mark.asyncio
    async def test_ldap_success_sets_session_cookie(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful LDAP bind issues the session cookie + commits."""
        controller = MagicMock()

        request = MagicMock()
        request.content_type = "application/x-www-form-urlencoded"

        async def _form() -> dict[str, str]:
            return {"username": "alice", "password": "secret"}

        request.form = _form
        request.query_params.get = MagicMock(return_value="")
        request.headers.get = MagicMock(return_value="liteset.local")

        settings = MagicMock()
        settings.auth_type = 2  # AUTH_LDAP
        settings.secret_key = "x" * 32
        settings.session_cookie_name = "session"
        settings.session_max_age = 3600

        session = _FakeAsyncSession()
        state = MagicMock()
        state.settings = settings
        state.session_factory = MagicMock(return_value=session)

        ldap_user = MagicMock()
        ldap_user.id = 7

        import superset.controllers.auth as auth_mod

        sm = MagicMock()

        async def _auth_ldap(*a: object, **k: object):
            return ldap_user

        sm.auth_user_ldap = _auth_ldap
        monkeypatch.setattr(auth_mod, "_build_session_manager", lambda *a, **k: sm)

        result = await _login_submit_fn(controller, request=request, state=state)

        assert isinstance(result, Redirect)
        assert result.url == "/"
        assert session.committed is True
        assert "session" in {c.key for c in result.cookies}

    @pytest.mark.asyncio
    async def test_ldap_failure_redirects_to_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed LDAP bind redirects to the login page (no session cookie)."""
        controller = MagicMock()

        request = MagicMock()
        request.content_type = "application/x-www-form-urlencoded"

        async def _form() -> dict[str, str]:
            return {"username": "alice", "password": "wrong"}

        request.form = _form
        request.query_params.get = MagicMock(return_value="")
        request.headers.get = MagicMock(return_value="liteset.local")

        settings = MagicMock()
        settings.auth_type = 2
        settings.secret_key = "x" * 32

        session = _FakeAsyncSession()
        state = MagicMock()
        state.settings = settings
        state.session_factory = MagicMock(return_value=session)

        import superset.controllers.auth as auth_mod

        sm = MagicMock()

        async def _auth_ldap(*a: object, **k: object):
            return None

        sm.auth_user_ldap = _auth_ldap
        monkeypatch.setattr(auth_mod, "_build_session_manager", lambda *a, **k: sm)

        result = await _login_submit_fn(controller, request=request, state=state)

        assert isinstance(result, Redirect)
        assert result.url.startswith("/login/")
        assert session.committed is False


class TestOAuthLoginRoute:
    """GET /login/oauth/{provider} initiates the Authorization-Code flow."""

    @pytest.mark.asyncio
    async def test_authorize_redirect_sets_state_cookie(self) -> None:
        """A configured provider yields a 302 to the IdP plus a state cookie."""
        controller = MagicMock()
        request = MagicMock()
        request.user = None
        request.query_params.get = MagicMock(return_value="")
        request.headers.get = MagicMock(return_value="liteset.local")

        state = MagicMock()
        state.settings = _oauth_settings()

        result = await _oauth_login_fn(
            controller, provider="keycloak", request=request, state=state
        )

        assert isinstance(result, Redirect)
        assert result.url.startswith("https://idp/auth?")
        assert "client_id=liteset" in result.url
        # A signed state cookie must be set for the callback to verify.
        cookie_keys = {c.key for c in result.cookies}
        assert "superset_oauth_state" in cookie_keys

    @pytest.mark.asyncio
    async def test_unknown_provider_redirects_to_login(self) -> None:
        """An unconfigured provider name redirects back to the login page."""
        controller = MagicMock()
        request = MagicMock()
        request.user = None
        request.query_params.get = MagicMock(return_value="")
        request.headers.get = MagicMock(return_value="liteset.local")

        state = MagicMock()
        state.settings = _oauth_settings()

        result = await _oauth_login_fn(
            controller, provider="does-not-exist", request=request, state=state
        )

        assert isinstance(result, Redirect)
        assert result.url == "/login/"

    @pytest.mark.asyncio
    async def test_non_oauth_auth_type_redirects_to_login(self) -> None:
        """When AUTH_TYPE != AUTH_OAUTH the route refuses the flow."""
        controller = MagicMock()
        request = MagicMock()
        request.user = None
        request.query_params.get = MagicMock(return_value="")
        request.headers.get = MagicMock(return_value="liteset.local")

        settings = _oauth_settings()
        settings.auth_type = 1  # AUTH_DB
        state = MagicMock()
        state.settings = settings

        result = await _oauth_login_fn(
            controller, provider="keycloak", request=request, state=state
        )

        assert isinstance(result, Redirect)
        assert result.url == "/login/"


class TestOAuthAuthorizedRoute:
    """GET /oauth-authorized/{provider} completes the flow + sets session."""

    @pytest.mark.asyncio
    async def test_successful_callback_sets_session_cookie(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful callback issues the Liteset session cookie + commits."""
        controller = MagicMock()

        request = MagicMock()
        request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {"code": "the-code", "state": "st"}.get(k, d)
        )
        request.cookies.get = MagicMock(return_value="st")
        request.headers.get = MagicMock(return_value="liteset.local")
        request.url.scheme = "https"

        session = _FakeAsyncSession()
        state = MagicMock()
        state.settings = _oauth_settings()
        state.session_factory = MagicMock(return_value=session)

        # Patch the backend factory so no real HTTP/DB happens.
        import superset.controllers.auth as auth_mod

        authed_user = MagicMock()
        authed_user.id = 42

        class _FakeBackend:
            def __init__(self, *a: object, **k: object) -> None:
                pass

            async def handle_callback(self, *a: object, **k: object):
                return authed_user, "/dashboard/list/"

        monkeypatch.setattr(
            auth_mod, "_make_oauth_backend", lambda *a, **k: _FakeBackend()
        )

        result = await _oauth_authorized_fn(
            controller, provider="keycloak", request=request, state=state
        )

        assert isinstance(result, Redirect)
        assert result.url == "/dashboard/list/"
        assert session.committed is True
        cookie_keys = {c.key for c in result.cookies}
        assert "session" in cookie_keys

    @pytest.mark.asyncio
    async def test_callback_without_code_redirects_to_login(self) -> None:
        """A callback missing the auth code redirects to the login page."""
        controller = MagicMock()
        request = MagicMock()
        request.query_params.get = MagicMock(return_value="")
        request.cookies.get = MagicMock(return_value="")
        request.headers.get = MagicMock(return_value="liteset.local")
        request.url.scheme = "https"

        state = MagicMock()
        state.settings = _oauth_settings()

        result = await _oauth_authorized_fn(
            controller, provider="keycloak", request=request, state=state
        )

        assert isinstance(result, Redirect)
        assert result.url.startswith("/login/")

    @pytest.mark.asyncio
    async def test_failed_auth_redirects_to_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the backend returns no user, redirect to login (no cookie)."""
        controller = MagicMock()
        request = MagicMock()
        request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {"code": "c", "state": "st"}.get(k, d)
        )
        request.cookies.get = MagicMock(return_value="st")
        request.headers.get = MagicMock(return_value="liteset.local")
        request.url.scheme = "https"

        session = _FakeAsyncSession()
        state = MagicMock()
        state.settings = _oauth_settings()
        state.session_factory = MagicMock(return_value=session)

        import superset.controllers.auth as auth_mod

        class _FakeBackend:
            async def handle_callback(self, *a: object, **k: object):
                return None, ""

        monkeypatch.setattr(
            auth_mod, "_make_oauth_backend", lambda *a, **k: _FakeBackend()
        )

        result = await _oauth_authorized_fn(
            controller, provider="keycloak", request=request, state=state
        )

        assert isinstance(result, Redirect)
        assert result.url.startswith("/login/")
        assert session.committed is False

    @pytest.mark.asyncio
    async def test_callback_external_next_is_not_open_redirect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An external ``next`` from the flow must collapse to '/' (no open
        redirect), even on a successful login."""
        controller = MagicMock()
        request = MagicMock()
        request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {"code": "c", "state": "st"}.get(k, d)
        )
        request.cookies.get = MagicMock(return_value="st")
        request.headers.get = MagicMock(return_value="liteset.local")
        request.url.scheme = "https"

        session = _FakeAsyncSession()
        state = MagicMock()
        state.settings = _oauth_settings()
        state.session_factory = MagicMock(return_value=session)

        import superset.controllers.auth as auth_mod

        authed_user = MagicMock()
        authed_user.id = 42

        class _FakeBackend:
            async def handle_callback(self, *a: object, **k: object):
                return authed_user, "https://evil.example.com/phish"

        monkeypatch.setattr(
            auth_mod, "_make_oauth_backend", lambda *a, **k: _FakeBackend()
        )

        result = await _oauth_authorized_fn(
            controller, provider="keycloak", request=request, state=state
        )

        assert isinstance(result, Redirect)
        assert result.url == "/"
        assert "evil.example.com" not in result.url

    @pytest.mark.asyncio
    async def test_callback_backend_error_redirects_to_login_no_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the backend raises (state/CSRF mismatch, id_token signature
        failure), the callback redirects to login and does NOT commit/issue a
        session."""
        from superset.security.auth.oauth import OAuthCallbackError

        controller = MagicMock()
        request = MagicMock()
        request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {"code": "c", "state": "st"}.get(k, d)
        )
        request.cookies.get = MagicMock(return_value="st")
        request.headers.get = MagicMock(return_value="liteset.local")
        request.url.scheme = "https"

        session = _FakeAsyncSession()
        state = MagicMock()
        state.settings = _oauth_settings()
        state.session_factory = MagicMock(return_value=session)

        import superset.controllers.auth as auth_mod

        class _FakeBackend:
            async def handle_callback(self, *a: object, **k: object):
                raise OAuthCallbackError("OAuth state mismatch")

        monkeypatch.setattr(
            auth_mod, "_make_oauth_backend", lambda *a, **k: _FakeBackend()
        )

        result = await _oauth_authorized_fn(
            controller, provider="keycloak", request=request, state=state
        )

        assert isinstance(result, Redirect)
        assert result.url.startswith("/login/")
        assert session.committed is False
        assert "session" not in {c.key for c in result.cookies}
