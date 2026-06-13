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
"""Machine (server-to-server) authentication for headless browsers.

Ported from ``superset_old/utils/machine_auth.py`` to the Litestar/ASGI
runtime.  The original implementation minted a session cookie via
the upstream ``login_user`` helper + ``app.session_interface.save_session``.
In Liteset, sessions are stateless JWTs minted by
``superset.controllers.auth._create_session_cookie`` and decoded by
``SupersetAuthMiddleware._authenticate_cookie``.  This module mints the
same JWT shape so that any cookie returned here is accepted by the
running Liteset backend (and by an embedded headless browser hitting it).

Architectural adaptations vs. the original:

* ``MachineAuthProviderFactory.init_app`` now accepts either a Litestar
  ``Litestar`` instance (reading ``app.state.settings``) or a
  ``SupersetSettings`` directly via ``init_settings``.  The original
  read ``app.config["MACHINE_AUTH_PROVIDER_CLASS"]`` from the app.
* ``get_auth_cookies`` mints a JWT using the configured ``secret_key``
  and ``session_max_age`` rather than spinning up a test request
  context.  The cookie name is ``settings.session_cookie_name``
  (default ``"session"``), matching the auth controller and middleware.
* ``get_cookies`` retains the original three-branch logic (user → mint,
  request cookies → forward, otherwise empty).  In a Celery / worker
  context there is no request, so the empty-dict branch is hit, which
  matches what the original did when invoked outside a request
  context.
* ``authenticate_webdriver`` and ``authenticate_browser_context`` are
  ported 1:1 — the Selenium / Playwright APIs they call are
  framework-agnostic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, cast, TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import jwt

from superset.utils.class_utils import load_class_from_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext
    from selenium.webdriver.remote.webdriver import WebDriver

    from superset.config import SupersetSettings


# Default JWT lifetime when no settings are wired up (matches
# ``SupersetSettings.session_max_age`` default of 31 days).
_DEFAULT_SESSION_MAX_AGE: int = 2678400


def _extract_secret_key(settings: SupersetSettings) -> str:
    """Pull the raw secret-key string out of ``SupersetSettings``.

    ``secret_key`` may be a ``pydantic.SecretStr`` — call
    ``get_secret_value()`` when present, otherwise coerce to ``str``.
    """
    raw = getattr(settings, "secret_key", "")
    if hasattr(raw, "get_secret_value"):
        return raw.get_secret_value()
    return str(raw)


class MachineAuthProvider:
    """Default machine-auth provider.

    Instantiated by :class:`MachineAuthProviderFactory` from the class
    path configured under ``MACHINE_AUTH_PROVIDER_CLASS`` (default:
    this class).  Operators wanting a different cookie/header strategy
    point ``MACHINE_AUTH_PROVIDER_CLASS`` at their own subclass.
    """

    def __init__(
        self,
        auth_webdriver_func_override: Callable[
            ["WebDriver | BrowserContext", Any], "WebDriver | BrowserContext"
        ]
        | None = None,
    ) -> None:
        # Allows the ``authenticate_webdriver`` /
        # ``authenticate_browser_context`` body to be overridden via
        # config (``WEBDRIVER_AUTH_FUNC``) without subclassing the
        # whole provider.  Mirrors the original argument name.
        self._auth_webdriver_func_override = auth_webdriver_func_override

        # Bound by ``MachineAuthProviderFactory.init_settings`` so that
        # the provider can read ``WEBDRIVER_BASEURL``, ``SECRET_KEY``,
        # ``SESSION_COOKIE_NAME`` and ``PERMANENT_SESSION_LIFETIME``
        # without depending on a ``current_app`` proxy.
        self._settings: SupersetSettings | None = None

    # ------------------------------------------------------------------
    # Settings wiring
    # ------------------------------------------------------------------

    def bind_settings(self, settings: SupersetSettings) -> None:
        """Attach a ``SupersetSettings`` instance to this provider.

        Called by :class:`MachineAuthProviderFactory.init_settings`.
        Keeping this as a public method (vs. constructor-only) preserves
        the original ``__init__(auth_webdriver_func)`` signature, so
        custom subclasses overriding ``__init__`` keep working.
        """
        self._settings = settings

    # ------------------------------------------------------------------
    # WebDriver / BrowserContext authentication (ported 1:1)
    # ------------------------------------------------------------------

    def authenticate_webdriver(
        self,
        driver: "WebDriver",
        user: Any,
    ) -> "WebDriver":
        """Sets a session cookie on a Selenium driver.

        Returns the driver passed in (fluent), matching the original.
        """
        # Short-circuit when an override callable is configured. The override
        # is shared with ``authenticate_browser_context`` so its declared return
        # is the WebDriver|BrowserContext union; in this path it returns a
        # WebDriver.
        if self._auth_webdriver_func_override:
            return cast("WebDriver", self._auth_webdriver_func_override(driver, user))

        # Setting cookies requires doing a request first (Selenium quirk)
        driver.get(self._headless_url("/login/"))

        cookies = self.get_cookies(user)

        for cookie_name, cookie_val in cookies.items():
            driver.add_cookie({"name": cookie_name, "value": cookie_val})

        return driver

    def authenticate_browser_context(
        self,
        browser_context: "BrowserContext",
        user: Any,
    ) -> "BrowserContext":
        """Sets a session cookie on a Playwright ``BrowserContext``."""
        if self._auth_webdriver_func_override:
            return cast(
                "BrowserContext",
                self._auth_webdriver_func_override(browser_context, user),
            )

        baseurl = self._webdriver_baseurl()
        url = urlparse(baseurl)

        # Setting cookies requires doing a request first
        page = browser_context.new_page()
        page.goto(self._headless_url("/login/"))

        cookies = self.get_cookies(user)

        browser_context.clear_cookies()
        browser_context.add_cookies(
            [
                {
                    "name": cookie_name,
                    "value": cookie_val,
                    "domain": url.netloc,
                    "path": "/",
                    "sameSite": "Lax",
                    "httpOnly": True,
                }
                for cookie_name, cookie_val in cookies.items()
            ]
        )
        return browser_context

    # ------------------------------------------------------------------
    # Cookie minting
    # ------------------------------------------------------------------

    def get_cookies(self, user: Any | None) -> dict[str, str]:
        """Resolve cookies for the given user.

        Mirrors the original three-branch behaviour:

        1. ``user`` provided  -> mint a session cookie via
           ``get_auth_cookies``.
        2. No user, but a ``request.cookies`` is available  ->
           forward those cookies.  In Liteset we have no global request
           proxy in a Celery worker context, so this branch is empty
           by design (the same effective behaviour the original
           had when invoked from a worker without a request context).
        3. Otherwise, return an empty dict.
        """
        if user:
            return self.get_auth_cookies(user)
        return {}

    def get_auth_cookies(self, user: Any) -> dict[str, str]:
        """Mint a session cookie that ``SupersetAuthMiddleware`` accepts.

        Original behaviour:
            * call the upstream ``login_user(user)`` inside a
              ``test_request_context``,
            * run ``app.process_response`` so ``after_request`` hooks
              (e.g. websocket JWT auth) populate ``Set-Cookie`` headers,
            * extract every ``Set-Cookie`` from the mock response and
              return them as a dict.

        Adapted behaviour (Liteset):
            * mint a JWT with ``{"user_id": <id>, "iat": ..., "exp": ...}``
              using ``SECRET_KEY`` / ``HS256``, exactly matching what
              ``superset.controllers.auth._create_session_cookie`` and
              ``SupersetAuthMiddleware._authenticate_cookie`` use,
            * return ``{<session_cookie_name>: <jwt>}``.

        This is the closest 1:1 functional equivalent: a single cookie,
        named after ``SESSION_COOKIE_NAME``, that re-authenticates the
        downstream request as the given user.
        """
        user_id = self._extract_user_id(user)
        if user_id is None:
            return {}

        settings = self._settings
        if settings is None:
            # Provider was never wired up via the factory.  Without
            # access to SECRET_KEY we cannot mint a verifiable cookie,
            # so fall back to an empty cookie set rather than a
            # silently-invalid one.  Logged as a warning so misconfig
            # is loud.
            logger.warning(
                "MachineAuthProvider used without bound settings; "
                "returning empty auth cookies",
            )
            return {}

        cookie_name = getattr(settings, "session_cookie_name", "session") or "session"
        max_age = getattr(settings, "session_max_age", _DEFAULT_SESSION_MAX_AGE) or (
            _DEFAULT_SESSION_MAX_AGE
        )
        secret_key = _extract_secret_key(settings)
        if not secret_key:
            logger.warning(
                "MachineAuthProvider has no SECRET_KEY configured; "
                "returning empty auth cookies",
            )
            return {}

        now = datetime.now(timezone.utc)
        payload = {
            "user_id": int(user_id),
            "iat": now,
            "exp": now + timedelta(seconds=int(max_age)),
        }
        token = jwt.encode(payload, secret_key, algorithm="HS256")
        return {cookie_name: token}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_user_id(user: Any) -> int | None:
        """Pull a numeric user id off whatever ``user`` shape we get.

        Supports ORM ``User``, ``CachedUser`` dataclass, plain dicts,
        and anything else exposing an ``id`` attribute / key.
        """
        if user is None:
            return None
        for attr in ("id", "user_id"):
            value = getattr(user, attr, None)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        if isinstance(user, dict):
            for key in ("id", "user_id"):
                if key in user:
                    try:
                        return int(user[key])
                    except (TypeError, ValueError):
                        continue
        return None

    def _webdriver_baseurl(self) -> str:
        """Read ``WEBDRIVER_BASEURL`` from bound settings (or empty)."""
        settings = self._settings
        if settings is None:
            return ""
        return getattr(settings, "webdriver_baseurl", "") or ""

    def _headless_url(self, path: str) -> str:
        """``urljoin(WEBDRIVER_BASEURL, path)`` — mirrors
        ``utils.urls.headless_url``."""
        return urljoin(self._webdriver_baseurl(), path)


class MachineAuthProviderFactory:
    """Factory that resolves and caches the configured provider.

    Behaves like the original factory: ``init_app`` is called once
    at startup with the running app; afterwards ``.instance`` returns
    the singleton provider for the rest of the process lifetime.

    In Liteset we accept either a Litestar ``Litestar`` (and pull
    settings off ``app.state.settings``) or a ``SupersetSettings``
    directly via ``init_settings``.  This keeps the call-site in the
    Litestar startup hook idiomatic while preserving the legacy method
    name expected by any code that still calls ``init_app(app)``.
    """

    def __init__(self) -> None:
        self._auth_provider: MachineAuthProvider | None = None

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def init_app(self, app: Any) -> None:
        """Initialise from a Litestar ``Litestar`` instance.

        Reads ``SupersetSettings`` from ``app.state.settings`` and
        delegates to :meth:`init_settings`.  The method is named
        ``init_app`` (not ``init_litestar``) to preserve the historic
        contract used by any caller that hasn't been migrated yet.
        """
        settings = getattr(getattr(app, "state", None), "settings", None)
        if settings is None:
            raise RuntimeError(
                "MachineAuthProviderFactory.init_app requires a Litestar "
                "instance with `app.state.settings` populated; got "
                f"{type(app).__name__!r} without settings"
            )
        self.init_settings(settings)

    def init_settings(self, settings: SupersetSettings) -> None:
        """Resolve the provider class from settings and instantiate it."""
        provider_class_path = getattr(
            settings,
            "machine_auth_provider_class",
            "superset.utils.machine_auth.MachineAuthProvider",
        )
        provider_class = load_class_from_name(provider_class_path)
        webdriver_auth_func = getattr(settings, "webdriver_auth_func", None)
        provider = provider_class(webdriver_auth_func)
        # Bind settings so the provider can read SECRET_KEY,
        # SESSION_COOKIE_NAME, WEBDRIVER_BASEURL, etc.
        if hasattr(provider, "bind_settings"):
            provider.bind_settings(settings)
        self._auth_provider = provider

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    @property
    def instance(self) -> MachineAuthProvider:
        """Return the configured provider.

        Matches the original behaviour: returns ``None`` (typed as
        ``MachineAuthProvider`` for caller convenience) if the factory
        was never initialised.  Callers downstream — webdriver helpers,
        Celery report task — guard via ``AttributeError`` /
        ``hasattr`` exactly like the original.
        """
        return self._auth_provider  # type: ignore[return-value]
