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
"""Legacy session cookie decoder for Strangler Fig coexistence.

Decodes itsdangerous URLSafeTimedSerializer signed session cookies
created by the legacy WSGI login layer. This allows Litestar's AuthMiddleware
to authenticate users who logged in through the legacy frontend without
requiring a separate login flow.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, cast

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)


class FlaskSessionDecoder:
    """Decodes itsdangerous-signed legacy session cookies.

    Args:
        secret_key: The SECRET_KEY used by the legacy application.
        salt: The salt used for the session cookie (default: "cookie-session").
        max_age: Maximum age in seconds for the cookie (default: 31 days).
    """

    # 31 days in seconds — matches the legacy PERMANENT_SESSION_LIFETIME default
    DEFAULT_MAX_AGE: int = 86400 * 31

    def __init__(
        self,
        secret_key: str,
        *,
        salt: str = "cookie-session",
        max_age: int | None = DEFAULT_MAX_AGE,
    ) -> None:
        # 1:1 with the legacy ``SecureCookieSessionInterface
        # .get_signing_serializer`` (upstream sessions.py):
        # ``key_derivation="hmac"`` + SHA-1 digest.
        # itsdangerous' defaults (``django-concat`` key derivation) produce
        # signatures incompatible with the legacy cookies, so without
        # ``signer_kwargs`` every real legacy session cookie fails with
        # BadSignature.  The legacy ``TaggedJSONSerializer`` payload tags are
        # post-processed by :meth:`_untag` (this package has no legacy WSGI
        # dependency — the Strangler-Fig fallback is gone).
        self._serializer = URLSafeTimedSerializer(
            secret_key,
            salt=salt,
            signer_kwargs={
                "key_derivation": "hmac",
                "digest_method": hashlib.sha1,
            },
        )
        self._max_age = max_age

    @classmethod
    def _untag(cls, value: Any) -> Any:
        """Resolve legacy ``TaggedJSONSerializer`` tags to plain values.

        Mirrors the upstream ``json.tag`` for the tag forms that can appear in a
        session payload: ``{" t": [...]}`` (tuple), ``{" u": hex}`` (UUID),
        ``{" b": base64}`` (bytes), ``{" m": str}`` (Markup → str),
        ``{" d": RFC-822 date}`` (kept as the string — callers only read
        scalar session keys), ``{" di": {...}}`` (dict whose keys collide
        with tag names).
        """
        if isinstance(value, dict):
            if len(value) == 1:
                tag, inner = next(iter(value.items()))
                if tag == " t":
                    return tuple(cls._untag(v) for v in inner)
                if tag == " u":
                    import uuid as _uuid

                    return _uuid.UUID(inner)
                if tag == " b":
                    import base64

                    return base64.b64decode(inner)
                if tag in (" m", " d"):
                    return inner
                if tag == " di":
                    return {k: cls._untag(v) for k, v in inner.items()}
            return {k: cls._untag(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._untag(v) for v in value]
        return value

    def decode(self, cookie_value: str | None) -> dict[str, Any] | None:
        """Decode a legacy session cookie.

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
                return cast("dict[str, Any]", self._untag(payload))
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
        """Extract user_id from a legacy session cookie.

        The legacy login layer stores the user ID as "_user_id" in the session.

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
