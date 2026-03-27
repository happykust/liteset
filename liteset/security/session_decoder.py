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
"""Flask session cookie decoder for Strangler Fig coexistence.

Decodes itsdangerous URLSafeTimedSerializer signed session cookies
created by Flask/Flask-Login. This allows Litestar's AuthMiddleware
to authenticate users who logged in through the Flask frontend without
requiring a separate login flow.
"""

from __future__ import annotations

import logging
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)


class FlaskSessionDecoder:
    """Decodes itsdangerous-signed Flask session cookies.

    Args:
        secret_key: The SECRET_KEY used by the Flask application.
        salt: The salt used for the session cookie (default: "cookie-session").
        max_age: Maximum age in seconds for the cookie (default: 31 days).
    """

    # 31 days in seconds — matches Flask's PERMANENT_SESSION_LIFETIME default
    DEFAULT_MAX_AGE: int = 86400 * 31

    def __init__(
        self,
        secret_key: str,
        *,
        salt: str = "cookie-session",
        max_age: int | None = DEFAULT_MAX_AGE,
    ) -> None:
        self._serializer = URLSafeTimedSerializer(secret_key, salt=salt)
        self._max_age = max_age

    def decode(self, cookie_value: str | None) -> dict[str, Any] | None:
        """Decode a Flask session cookie.

        Args:
            cookie_value: The raw cookie string from the request.

        Returns:
            The decoded session dict, or None if decoding fails.
        """
        if not cookie_value:
            return None
        try:
            payload = self._serializer.loads(cookie_value, max_age=self._max_age)
            if isinstance(payload, dict):
                return payload
            logger.warning("Session payload is not a dict: %s", type(payload))
            return None
        except SignatureExpired:
            logger.debug("Session cookie expired")
            return None
        except BadSignature:
            logger.debug("Invalid session cookie signature")
            return None
        except Exception:
            logger.debug("Failed to decode session cookie", exc_info=True)
            return None

    def get_user_id(self, cookie_value: str | None) -> int | None:
        """Extract user_id from a Flask session cookie.

        Flask-Login stores the user ID as "_user_id" in the session.

        Args:
            cookie_value: The raw cookie string.

        Returns:
            The user ID as an integer, or None if not found.
        """
        payload = self.decode(cookie_value)
        if payload is None:
            return None
        raw_id = payload.get("_user_id")
        if raw_id is None:
            return None
        try:
            return int(raw_id)
        except (ValueError, TypeError):
            logger.warning("Invalid _user_id in session: %s", raw_id)
            return None
