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
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from litestar import Controller, get, post, Request
from litestar.datastructures import State
from litestar.response import Redirect, Template

from superset.config import SupersetSettings
from superset.controllers.spa import _build_bootstrap_data
from superset.middleware.auth import UnauthenticatedUser
from superset.utils.password import check_password_hash as _check_password_hash

logger = logging.getLogger(__name__)

# Default session lifetime in seconds (31 days, matches
# legacy PERMANENT_SESSION_LIFETIME).
# Overridden at runtime by ``settings.session_max_age``.
_DEFAULT_SESSION_MAX_AGE: int = 86400 * 31

# Pre-computed hash used for timing balance when the user is not found or
# inactive.  Mirrors Flask-AppBuilder's ``AUTH_DB_FAKE_PASSWORD_HASH_CHECK``.
# Format: scrypt:32768:8:1 (werkzeug 3.0+ default).
_FAKE_PASSWORD_HASH = (
    "scrypt:32768:8:1$FakeTimingSalt01$"
    "0000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000"
)


def _get_secret_key(settings: SupersetSettings) -> str:
    """Extract the secret key string from settings."""
    secret_key = settings.secret_key
    if hasattr(secret_key, "get_secret_value"):
        secret_key = secret_key.get_secret_value()
    return str(secret_key)


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

        # If already authenticated, redirect to home
        if user is not None and getattr(user, "is_authenticated", False):
            return Redirect(path="/")

        bootstrap = _build_bootstrap_data(UnauthenticatedUser(), settings)
        import json

        return Template(
            template_name="spa.html",
            context={
                "bootstrap_data": json.dumps(bootstrap),
                "entry": "spa",
                "title": "Superset",
                "assets_prefix": settings.static_assets_prefix,
                "standalone_mode": False,
                "favicons": [
                    {"href": "/static/assets/images/favicon.png"}
                ],
                "csrf_token": "",
            },
        )

    @post(
        ["/login/", "/login"],
        exclude_from_auth=True,
        opt={"exclude_from_csrf": True},
    )
    async def login_submit(
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
        from sqlalchemy import update

        from superset.models.security import User
        from superset.security.dao import AsyncSecurityDAO

        settings: SupersetSettings = state.settings
        form_data = await request.form()
        username = str(form_data.get("username", "")).strip()
        password = str(form_data.get("password", ""))

        if not username or not password:
            return Redirect(path="/login/")

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
                first_user_login_count = (
                    getattr(first_user, "login_count", 0) or 0
                )

            user_by_name = await dao.get_user_by_username(username)
            if user_by_name is None:
                user_obj = await dao.get_user_by_email(username)
            else:
                _ = await dao.get_user_by_email(username)
                user_obj = user_by_name

            if user_obj is not None:
                user_id = user_obj.id
                user_active = bool(
                    getattr(user_obj, "active", False)
                )
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
                        await session.execute(
                            update(User)
                            .where(User.id == first_user_id)
                            .values(
                                login_count=first_user_login_count,
                            )
                        )
                        await session.commit()
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Noop user update failed"
                    )
            return Redirect(path="/login/")

        # ------------------------------------------------------------------
        # Verify password (FAB stores werkzeug-hashed passwords)
        # ------------------------------------------------------------------
        if (
            not user_password
            or not _check_password_hash(user_password, password)
        ):
            logger.debug(
                "Login failed: wrong password for '%s'",
                username,
            )
            try:
                async with session_factory() as session:
                    await session.execute(
                        update(User)
                        .where(User.id == user_id)
                        .values(
                            fail_login_count=User.fail_login_count + 1,
                        )
                    )
                    await session.commit()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Failed to update fail_login_count"
                )
            return Redirect(path="/login/")

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
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(
                        last_login=datetime.now(),
                        login_count=User.login_count + 1,
                        fail_login_count=0,
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to update login metadata for '%s'",
                username,
            )

        logger.info("User '%s' logged in successfully", username)

        # Redirect to home; set session cookie via response headers
        redirect = Redirect(path="/")
        redirect.cookies.append(
            _make_session_cookie(
                cookie_name,
                cookie_value,
                max_age=session_max_age,
                secure=getattr(settings, "session_cookie_secure", False),
                httponly=getattr(settings, "session_cookie_httponly", True),
                samesite=getattr(settings, "session_cookie_samesite", "lax"),
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
            from superset.security.session_decoder import SessionDecoder

            secret_key = _get_secret_key(settings)
            decoder = SessionDecoder(secret_key=secret_key)
            cookie_value = request.cookies.get(cookie_name)
            user_id = decoder.decode(cookie_value)
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
                samesite=getattr(settings, "session_cookie_samesite", "lax"),
            ),
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
        samesite=samesite,
    )
