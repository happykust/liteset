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
"""Authentication controller — login/logout endpoints.

Provides a simple HTML login form and JWT session cookie management
that ``SupersetAuthMiddleware`` decodes via ``SessionDecoder``.
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import jwt
from litestar import Controller, get, post, Request
from litestar.datastructures import State
from litestar.response import Redirect, Template

from superset.config import SupersetSettings
from superset.controllers.spa import _build_bootstrap_data
from superset.db.daos.user import AsyncUserDAO
from superset.middleware.auth import UnauthenticatedUser
from superset.utils.password import check_password_hash as _check_password_hash

logger = logging.getLogger(__name__)

# Default session lifetime in seconds (31 days, matches
# legacy PERMANENT_SESSION_LIFETIME).
# Overridden at runtime by ``settings.session_max_age``.
_DEFAULT_SESSION_MAX_AGE: int = 86400 * 31

# Flash message matching Flask-AppBuilder's invalid_login_message.
_INVALID_LOGIN_MESSAGE: str = "Invalid login. Please try again."

# Cookie name for one-shot flash messages (read once then cleared).
_FLASH_COOKIE_NAME: str = "_flash"

# Pre-computed hash used for timing balance when the user is not found or
# inactive.  Mirrors Flask-AppBuilder's ``AUTH_DB_FAKE_PASSWORD_HASH_CHECK``.
# Format: scrypt:32768:8:1 (werkzeug 3.0+ default).
_FAKE_PASSWORD_HASH = (
    "scrypt:32768:8:1$FakeTimingSalt01$"  # noqa: S105
    "0000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000"
)


def _is_safe_redirect_url(url: str, request_host: str = "") -> bool:
    """Check whether a redirect URL is safe (relative or same-host).

    Mirrors Flask-AppBuilder's ``is_safe_redirect_url`` but without
    Flask dependencies.  Prevents open-redirect attacks by rejecting
    URLs with external hosts or non-http(s) schemes.

    Args:
        url: The candidate redirect URL.
        request_host: The ``Host`` header of the current request,
            used to allow same-origin absolute URLs.
    """
    if not url:
        return False
    if url.startswith("///"):
        return False
    try:
        url_info = urlparse(url)
    except ValueError:
        return False
    if not url_info.netloc and url_info.scheme:
        return False
    # Reject control characters at start (e.g. ``\x00//evil.com``)
    if unicodedata.category(url[0])[0] == "C":
        return False
    scheme = url_info.scheme
    if not url_info.scheme and url_info.netloc:
        scheme = "http"
    valid_schemes = ("http", "https")
    # Allow relative URLs (no netloc) or same-host absolute URLs
    if url_info.netloc and url_info.netloc != request_host:
        return False
    return not scheme or scheme in valid_schemes


def _get_safe_redirect(url: str, request_host: str = "", fallback: str = "/") -> str:
    """Return *url* if safe, otherwise return *fallback*.

    Mirrors Flask-AppBuilder's ``get_safe_redirect``.
    """
    if url and _is_safe_redirect_url(url, request_host=request_host):
        return url
    return fallback


def _get_secret_key(settings: SupersetSettings) -> str:
    """Extract the secret key string from settings."""
    raw_key = settings.secret_key
    if hasattr(raw_key, "get_secret_value"):
        return raw_key.get_secret_value()
    return str(raw_key)


def _create_session_cookie(
    secret_key: str,
    user_id: int,
    max_age_seconds: int = _DEFAULT_SESSION_MAX_AGE,
) -> str:
    """Create a JWT session cookie value.

    Produces a signed JWT that ``SessionDecoder.decode()`` can verify.
    Payload: ``{"user_id": <int>, "iat": <timestamp>, "exp": <timestamp>}``.

    *max_age_seconds* controls the JWT expiry and should come from
    ``settings.session_max_age``.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": user_id,
        "iat": now,
        "exp": now + timedelta(seconds=max_age_seconds),
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


def _clear_flash_cookies(
    response: Template,
    flash_raw: str | None,
    flash_danger_raw: str | None,
) -> None:
    """Expire flash cookies so each message shows exactly once."""
    if flash_raw or flash_danger_raw:
        from litestar.datastructures import Cookie

        if flash_raw:
            response.cookies.append(
                Cookie(key=_FLASH_COOKIE_NAME, value="", max_age=0, path="/")
            )
        if flash_danger_raw:
            response.cookies.append(
                Cookie(key="_flash_danger", value="", max_age=0, path="/")
            )


class AuthController(Controller):
    """Login / logout endpoints."""

    path = "/"
    tags = ["Auth"]

    @get(
        ["/login/", "/login"],
        exclude_from_auth=True,
    )
    async def login_page(
        self,
        request: Request[Any, Any, Any],
        state: State,
    ) -> Template | Redirect:
        """GET /login/ -- render the SPA shell (React handles the login form)."""
        settings: SupersetSettings = state.settings
        try:
            user = request.user
        except Exception:  # noqa: BLE001
            user = None

        # If already authenticated, redirect to home.
        # Mirrors FAB @no_cache wrapping ALL return paths of SupersetAuthView.login:
        # the early-return redirect(appbuilder.get_url_for_index) also received
        # Cache-Control/Pragma/Expires headers in the original.
        if user is not None and getattr(user, "is_authenticated", False):
            _redirect = Redirect(path="/")
            _redirect.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, max-age=0"
            )
            _redirect.headers["Pragma"] = "no-cache"
            _redirect.headers["Expires"] = "0"
            return _redirect

        # Read one-shot flash messages from cookie (set by failed POST /login/).
        flash_messages: list[list[str]] = []
        import urllib.parse

        flash_raw = request.cookies.get(_FLASH_COOKIE_NAME)
        if flash_raw:
            flash_messages.append(["warning", urllib.parse.unquote(flash_raw)])
        # "danger" category cookie set by register_activation errors --
        # mirrors Flask flash(msg, "danger") for registration not found /
        # add_user failed.
        flash_danger_raw = request.cookies.get("_flash_danger")
        if flash_danger_raw:
            flash_messages.append(["danger", urllib.parse.unquote(flash_danger_raw)])

        # Build anonymous user with Public role permissions (if configured),
        # matching Flask-AppBuilder behaviour where anonymous visitors see
        # the Public role's permissions in bootstrap data.
        anon_user = UnauthenticatedUser()
        role_name = getattr(settings, "auth_role_public", "")
        if role_name:
            from superset.security.dao import AsyncSecurityDAO

            session_factory = state.session_factory
            try:
                async with session_factory() as session:
                    dao = AsyncSecurityDAO(session)
                    permissions = await dao.get_permissions_for_role_name(role_name)
                    if permissions:
                        from superset.middleware.auth import _CachedRole

                        anon_user = UnauthenticatedUser(
                            roles=[_CachedRole(id=0, name=role_name)],
                            permissions=permissions,
                        )
            except Exception:  # noqa: BLE001
                logger.debug("Failed to load Public role permissions for login page")

        bootstrap = _build_bootstrap_data(
            anon_user,
            settings,
            flash_messages=flash_messages,
        )
        import json

        response = Template(
            template_name="spa.html",
            context={
                "bootstrap_data": json.dumps(bootstrap),
                "entry": "spa",
                "title": "Superset",
                "assets_prefix": settings.static_assets_prefix,
                "standalone_mode": False,
                "favicons": [{"href": "/static/assets/images/favicon.png"}],
                "csrf_token": "",
            },
        )
        # Mirrors Flask-AppBuilder's @no_cache decorator on the login view.
        # Sets Cache-Control: no-store, no-cache, must-revalidate, max-age=0;
        # Pragma: no-cache; Expires: 0.  Prevents browsers and proxies from
        # caching the login page (which could expose stale auth state).
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        # Clear the flash cookies so messages show only once.
        _clear_flash_cookies(response, flash_raw, flash_danger_raw)
        return response

    @post(
        ["/login/", "/login"],
        exclude_from_auth=True,
        opt={"exclude_from_csrf": True},
    )
    async def login_submit(  # noqa: C901
        self,
        request: Request[Any, Any, Any],
        state: State,
    ) -> Redirect:
        """POST /login/ -- authenticate and set session cookie.

        Mirrors Flask-AppBuilder ``auth_user_db`` behaviour:
        - Tries username first, then falls back to email lookup.
        - Always performs both lookups for timing balance.
        - On user-not-found / inactive: fake hash check + noop DB write
          to prevent timing-based user enumeration.

        On failure, redirects back to ``/login/`` -- the React SPA renders
        the login form and error feedback (via flash_messages in bootstrap).
        """
        from superset.security.dao import AsyncSecurityDAO

        settings: SupersetSettings = state.settings

        # Accept both form data and JSON body (Cypress sends JSON,
        # browser login form sends application/x-www-form-urlencoded).
        content_type = request.content_type or ""
        json_data: dict[str, Any] = {}
        form_data: Any = {}
        if "application/json" in content_type:
            try:
                json_data = await request.json() or {}
            except Exception:  # noqa: BLE001
                json_data = {}
            username = str(json_data.get("username", "")).strip()
            password = str(json_data.get("password", ""))
        else:
            form_data = await request.form()
            username = str(form_data.get("username", "")).strip()
            password = str(form_data.get("password", ""))

        # Read the ``next`` redirect target from query params or form data.
        # Mirrors FAB's ``get_safe_redirect(request.args.get("next", ""))``.
        next_url = request.query_params.get("next", "")
        if not next_url:
            if "application/json" in content_type:
                next_url = str(json_data.get("next", ""))
            else:
                next_url = str(form_data.get("next", ""))
        request_host = request.headers.get("host", "")
        safe_redirect_target = _get_safe_redirect(
            next_url, request_host=request_host, fallback="/"
        )

        if not username or not password:
            return _login_failed_redirect(next_url=next_url)

        session_factory = state.session_factory

        # ------------------------------------------------------------------
        # User lookup (mirrors FAB's auth_user_db timing balance)
        # Always perform both by-username and by-email queries so that the
        # total DB round-trips are identical regardless of the result.
        # ------------------------------------------------------------------
        # Extract all needed attributes inside the session scope
        # to avoid DetachedInstanceError after session closes.
        user_id: int | None = None
        user_active: bool = False
        user_password: str | None = None
        first_user_id: int | None = None
        first_user_login_count: int = 0

        async with session_factory() as session:
            dao = AsyncSecurityDAO(session)

            first_user = await dao.get_first_user()
            if first_user is not None:
                first_user_id = first_user.id
                first_user_login_count = getattr(first_user, "login_count", 0) or 0

            user_by_name = await dao.get_user_by_username(username)
            if user_by_name is None:
                user_obj = await dao.get_user_by_email(username)
            else:
                _ = await dao.get_user_by_email(username)
                user_obj = user_by_name

            if user_obj is not None:
                user_id = user_obj.id
                user_active = bool(getattr(user_obj, "active", False))
                user_password = getattr(user_obj, "password", None)

        if user_id is None or not user_active:
            # Spend time computing a hash to match the success path timing
            _check_password_hash(_FAKE_PASSWORD_HASH, "password")
            logger.debug(
                "Login failed: user '%s' %s",
                username,
                "not found" if user_id is None else "inactive",
            )
            if first_user_id is not None:
                try:
                    async with session_factory() as session:
                        user_dao = AsyncUserDAO(session)
                        await user_dao.update_login_count(
                            first_user_id, first_user_login_count
                        )
                        await session.commit()
                except Exception:  # noqa: BLE001
                    logger.debug("Noop user update failed")
            return _login_failed_redirect(next_url=next_url)

        # ------------------------------------------------------------------
        # Verify password (FAB stores werkzeug-hashed passwords)
        # ------------------------------------------------------------------
        if not user_password or not _check_password_hash(user_password, password):
            logger.debug(
                "Login failed: wrong password for '%s'",
                username,
            )
            try:
                async with session_factory() as session:
                    user_dao = AsyncUserDAO(session)
                    await user_dao.increment_fail_login_count(user_id)
                    await session.commit()
            except Exception:  # noqa: BLE001
                logger.debug("Failed to update fail_login_count")
            return _login_failed_redirect(next_url=next_url)

        # ------------------------------------------------------------------
        # Authentication successful -- create session cookie
        # ------------------------------------------------------------------
        secret_key = _get_secret_key(settings)
        session_max_age: int = getattr(
            settings, "session_max_age", _DEFAULT_SESSION_MAX_AGE
        )
        cookie_value = _create_session_cookie(
            secret_key, user_id, max_age_seconds=session_max_age
        )
        cookie_name = getattr(settings, "session_cookie_name", "session")

        # Update login metadata (best-effort)
        try:
            async with session_factory() as session:
                user_dao = AsyncUserDAO(session)
                await user_dao.record_successful_login(user_id)
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to update login metadata for '%s'",
                username,
            )

        logger.info("User '%s' logged in successfully", username)

        # Redirect to ``?next=`` target (validated) or home
        redirect = Redirect(path=safe_redirect_target)
        # Mirrors Flask-AppBuilder's @no_cache on the combined GET+POST login
        # handler — applies to every response path including POST success.
        redirect.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        redirect.headers["Pragma"] = "no-cache"
        redirect.headers["Expires"] = "0"
        redirect.cookies.append(
            _make_session_cookie(
                cookie_name,
                cookie_value,
                max_age=session_max_age,
                secure=getattr(settings, "session_cookie_secure", False),
                httponly=getattr(settings, "session_cookie_httponly", True),
                samesite=str(
                    getattr(settings, "session_cookie_samesite", "lax")
                ).lower(),
            ),
        )
        return redirect

    @get(
        ["/logout/", "/logout"],
        exclude_from_auth=True,
    )
    async def logout(
        self,
        request: Request[Any, Any, Any],
        state: State,
    ) -> Redirect:
        """GET /logout/ -- clear session cookie, redirect to login.

        Additionally invalidates the user's Redis cache and writes a
        token-blacklist timestamp so that any previously issued JWT
        access tokens (``type="access"``) are rejected by the auth
        middleware if their ``iat`` is earlier than the blacklist entry.
        """
        settings: SupersetSettings = state.settings
        cookie_name = getattr(settings, "session_cookie_name", "session")

        # Invalidate Redis cache and blacklist JWT tokens (best-effort)
        try:
            secret_key = _get_secret_key(settings)
            cookie_value = request.cookies.get(cookie_name)
            user_id: int | None = None

            # Try JWT decode first (Liteset-native session cookies)
            if cookie_value:
                try:
                    payload = jwt.decode(cookie_value, secret_key, algorithms=["HS256"])
                    user_id = payload.get("user_id")
                except Exception:  # noqa: BLE001, S110
                    pass

            # Fallback: itsdangerous (Flask legacy cookies)
            if user_id is None and cookie_value:
                from superset.security.session_decoder import FlaskSessionDecoder

                decoder = FlaskSessionDecoder(secret_key=secret_key)
                user_id = decoder.get_user_id(cookie_value)

            if user_id is not None:
                redis = getattr(state, "redis", None)
                if redis is not None:
                    # Invalidate cached user object
                    await redis.delete(f"auth:user:{user_id}")
                    # Write token blacklist timestamp so that the auth
                    # middleware rejects access tokens issued before this
                    # moment.  TTL matches the longest token lifetime
                    # (jwt_refresh_token_expires, default 30 days).
                    blacklist_ttl: int = getattr(
                        settings, "jwt_refresh_token_expires", 86400 * 30
                    )
                    now_ts = int(datetime.now(timezone.utc).timestamp())
                    await redis.set(
                        f"auth:token_blacklist:{user_id}",
                        str(now_ts),
                        ex=blacklist_ttl,
                    )
        except Exception:
            logger.debug("Failed to invalidate Redis cache on logout")

        redirect = Redirect(path="/login/")
        # Expire the session cookie by setting max_age=0
        redirect.cookies.append(
            _make_session_cookie(
                cookie_name,
                "",
                max_age=0,
                secure=getattr(settings, "session_cookie_secure", False),
                httponly=getattr(settings, "session_cookie_httponly", True),
                samesite=str(
                    getattr(settings, "session_cookie_samesite", "lax")
                ).lower(),
            ),
        )
        return redirect


def _login_failed_redirect(next_url: str = "") -> Redirect:
    """Return a redirect to /login/ with a flash cookie for the error message.

    Mirrors Flask-AppBuilder's ``flash(self.invalid_login_message, "warning")``
    followed by ``redirect(get_url_for_login_with(next_url))``.  The GET /login/
    handler reads the ``_flash`` cookie, includes it in
    ``bootstrap_data.common.flash_messages``, and clears the cookie so the
    message shows only once.

    If *next_url* is provided, it is preserved as a query parameter on the
    redirect so the user can be sent to their intended destination after a
    successful re-login attempt.
    """
    import urllib.parse

    from litestar.datastructures import Cookie

    login_path = "/login/"
    if next_url:
        login_path = f"/login/?next={urllib.parse.quote(next_url, safe='')}"
    redirect = Redirect(path=login_path)
    # Mirrors Flask-AppBuilder's @no_cache decorator on the combined GET+POST
    # login handler (FAB security/views.py AuthDBView.login is @no_cache).
    # The decorator wraps every response path — including POST failure redirects.
    redirect.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    redirect.headers["Pragma"] = "no-cache"
    redirect.headers["Expires"] = "0"
    redirect.cookies.append(
        Cookie(
            key=_FLASH_COOKIE_NAME,
            value=urllib.parse.quote(_INVALID_LOGIN_MESSAGE),
            max_age=60,  # short-lived; consumed on next GET /login/
            path="/",
            httponly=True,
            samesite="lax",
        )
    )
    return redirect


def _make_session_cookie(
    name: str,
    value: str,
    max_age: int | None = None,
    *,
    secure: bool = False,
    httponly: bool = True,
    samesite: str = "lax",
) -> Any:
    """Create a Cookie object for session management.

    Args:
        name: Cookie name.
        value: Cookie value (JWT token or empty string for expiry).
        max_age: Cookie max-age in seconds.
        secure: Set the ``Secure`` flag (should be True in production).
        httponly: Set the ``HttpOnly`` flag (default True).
        samesite: ``SameSite`` attribute (``"lax"``, ``"strict"``, ``"none"``).
    """
    from litestar.datastructures import Cookie

    return Cookie(
        key=name,
        value=value,
        max_age=max_age if max_age is not None else _DEFAULT_SESSION_MAX_AGE,
        path="/",
        httponly=httponly,
        secure=secure,
        samesite=samesite,  # type: ignore[arg-type]
    )
