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
"""Guest user model and guest token support.

Guest tokens enable embedded dashboard access without full user accounts.
Tokens are JWTs (HS256) containing user info, resource access list,
and Row Level Security rules.

Feature flag: EMBEDDED_SUPERSET must be enabled.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import jwt

logger = logging.getLogger(__name__)

# Token defaults
_GUEST_TOKEN_ALGORITHM: str = "HS256"  # noqa: S105
_GUEST_TOKEN_EXP_SECONDS: int = 300  # 5 minutes
_GUEST_TOKEN_TYPE: str = "guest"  # noqa: S105


@dataclass
class GuestUser:
    """Represents a guest user authenticated via JWT token.

    Guest users have restricted access — only to resources
    explicitly listed in the token's resources array.
    """

    id: int = 0
    username: str = "guest"
    first_name: str = ""
    last_name: str = ""
    is_authenticated: bool = True
    is_active: bool = True
    is_guest: bool = True
    roles: list[Any] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    rls_rules: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_token_payload(cls, payload: dict[str, Any]) -> GuestUser:
        """Create a GuestUser from a decoded JWT payload."""
        user_info = payload.get("user", {})
        return cls(
            username=user_info.get("username", "guest"),
            first_name=user_info.get("first_name", ""),
            last_name=user_info.get("last_name", ""),
            resources=payload.get("resources", []),
            rls_rules=payload.get("rls_rules", []),
        )


def create_guest_access_token(
    *,
    secret_key: str,
    user: dict[str, Any],
    resources: list[dict[str, Any]],
    rls: list[dict[str, Any]],
    exp_seconds: int = _GUEST_TOKEN_EXP_SECONDS,
) -> str:
    """Create a guest access JWT token.

    Args:
        secret_key: Application secret key for signing.
        user: Dict with user info (username, first_name, last_name).
        resources: List of resource access dicts (type, id).
        rls: List of Row Level Security rule dicts (clause).
        exp_seconds: Token expiry in seconds (default: 300).

    Returns:
        Encoded JWT token string.
    """
    now = int(time.time())
    payload = {
        "user": user,
        "resources": resources,
        "rls_rules": rls,
        "type": _GUEST_TOKEN_TYPE,
        "iat": now,
        "exp": now + exp_seconds,
    }
    return jwt.encode(payload, secret_key, algorithm=_GUEST_TOKEN_ALGORITHM)


def parse_guest_token(
    token: str,
    secret_key: str,
    algorithm: str = _GUEST_TOKEN_ALGORITHM,
) -> dict[str, Any] | None:
    """Parse and validate a guest JWT token.

    Args:
        token: Raw JWT token string.
        secret_key: Application secret key for validation.
        algorithm: JWT algorithm (default: HS256).

    Returns:
        Decoded payload dict if valid, None otherwise.
    """
    try:
        payload = jwt.decode(
            token,
            secret_key,
            algorithms=[algorithm],
        )
    except jwt.ExpiredSignatureError:
        logger.debug("Guest token expired")
        return None
    except jwt.InvalidTokenError:
        logger.debug("Invalid guest token")
        return None

    # Verify token type
    if payload.get("type") != _GUEST_TOKEN_TYPE:
        logger.debug(
            "Token type mismatch: expected '%s', got '%s'",
            _GUEST_TOKEN_TYPE,
            payload.get("type"),
        )
        return None

    return payload


def validate_guest_token_resources(
    resources: list[dict[str, Any]],
) -> list[str]:
    """Validate guest token resource entries.

    Verifies each resource has required fields (type, id) and that
    the resource type is supported (dashboard, chart).

    Args:
        resources: List of resource dicts from the guest token payload.

    Returns:
        List of validation error messages. Empty list means all valid.
    """
    errors: list[str] = []
    supported_types = {"dashboard", "chart"}
    for i, resource in enumerate(resources):
        if not isinstance(resource, dict):
            errors.append(f"Resource {i}: not a dict")
            continue
        res_type = resource.get("type")
        res_id = resource.get("id")
        if not res_type:
            errors.append(f"Resource {i}: missing 'type' field")
        elif res_type not in supported_types:
            errors.append(
                f"Resource {i}: unsupported type '{res_type}' "
                f"(expected one of {supported_types})"
            )
        if not res_id:
            errors.append(f"Resource {i}: missing 'id' field")
    return errors
