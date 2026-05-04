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
"""REMOTE_USER authentication backend.

1:1 port of :pymeth:`flask_appbuilder.security.manager.BaseSecurityManager.auth_user_remote_user`
(``Flask-AppBuilder/flask_appbuilder/security/manager.py:1407-1435``)
plus the corresponding view
(``Flask-AppBuilder/flask_appbuilder/security/views.py:978-996``).

The web tier (Apache, nginx, oauth2-proxy, …) is expected to populate a
trusted request header — by default ``REMOTE_USER`` — with the
authenticated principal's identity.  Liteset reads that header, finds
the matching ``ab_user`` row (creating it when ``AUTH_USER_REGISTRATION``
is on), and returns the user object.

There is no password check — REMOTE_USER explicitly trusts the upstream
proxy.  Deployments must verify the ``REMOTE_USER`` header is stripped
from every request that didn't come through the trusted proxy, otherwise
authentication can be spoofed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class RemoteUserAuthBackend:
    """Authenticate users from a trusted upstream proxy header.

    Wraps :pymeth:`AsyncSecurityManager.auth_user_remote_user` with the
    request-extraction glue that the FAB ``AuthRemoteUserView`` performs
    inline.  Used by :class:`SupersetAuthMiddleware` when
    ``AUTH_TYPE == AUTH_REMOTE_USER (3)``.
    """

    def __init__(self, security_manager: Any, *, settings: Any) -> None:
        self._sm = security_manager
        self._settings = settings

    @staticmethod
    def extract_username(headers: dict[str, str], env_var: str) -> str:
        """Pull the trusted-proxy username from the request headers.

        ASGI delivers HTTP headers lowercased.  The configured
        ``AUTH_REMOTE_USER_ENV_VAR`` (default ``REMOTE_USER``) is used to
        derive the matching HTTP header name (lowercase + ``_`` → ``-``).

        Only the single configured header is honoured — this matches FAB's
        ``AuthRemoteUserView`` which reads only the WSGI ``REMOTE_USER``
        environ key (or whatever ``AUTH_REMOTE_USER_ENV_VAR`` names).
        There are NO fallback headers: additional headers such as
        ``X-Forwarded-User`` or ``X-Remote-User`` are NOT trusted because
        they are typically client-controllable and would allow spoofing.
        """
        if not env_var:
            return ""
        # Derive the HTTP header name from the WSGI env-var name:
        # e.g. ``REMOTE_USER`` → ``remote-user``,
        #      ``HTTP_X_REMOTE_USER`` → ``http-x-remote-user`` → nginx strips
        #      the ``HTTP_`` prefix, yielding ``x-remote-user`` in ASGI.
        # We only check the single derived header name — no fallbacks.
        header_key = env_var.lower().replace("_", "-")
        return headers.get(header_key, "").strip()

    async def authenticate(self, headers: dict[str, str]) -> Any | None:
        """Resolve a user from the trusted-proxy header, if any.

        Returns ``None`` when the header is absent or the user cannot be
        authenticated (unknown user with self-registration disabled,
        inactive user, etc.).
        """
        env_var: str = (
            getattr(self._settings, "auth_remote_user_env_var", "REMOTE_USER")
            or "REMOTE_USER"
        )
        username = self.extract_username(headers, env_var)
        if not username:
            return None
        return await self._sm.auth_user_remote_user(
            username, settings=self._settings
        )
