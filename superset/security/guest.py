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
    active: int = 1
    is_guest: bool = True
    roles: list[Any] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    rls_rules: list[dict[str, Any]] = field(default_factory=list)
    permissions: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def from_token_payload(cls, payload: dict[str, Any]) -> GuestUser:
        """Create a GuestUser from a decoded JWT payload."""
        user_info = payload.get("user", {})
        resources = payload.get("resources", [])

        # Derive permissions from resource types
        permissions: set[tuple[str, str]] = set()
        for resource in resources:
            res_type = resource.get("type", "") if isinstance(resource, dict) else ""
            if res_type == "dashboard":
                permissions.update({("can_read", "Dashboard"), ("can_read", "Chart")})
            elif res_type == "chart":
                permissions.add(("can_read", "Chart"))

        return cls(
            username=user_info.get("username", "guest"),
            first_name=user_info.get("first_name", ""),
            last_name=user_info.get("last_name", ""),
            resources=resources,
            rls_rules=payload.get("rls_rules", []),
            permissions=permissions,
        )


def create_guest_access_token(
    *,
    secret_key: str,
    user: dict[str, Any],
    resources: list[dict[str, Any]],
    rls: list[dict[str, Any]],
    exp_seconds: int = _GUEST_TOKEN_EXP_SECONDS,
    audience: str = "",
) -> str:
    """Create a guest access JWT token.

    Args:
        secret_key: Application secret key for signing.
        user: Dict with user info (username, first_name, last_name).
        resources: List of resource access dicts (type, id).
        rls: List of Row Level Security rule dicts (clause).
        exp_seconds: Token expiry in seconds (default: 300).
        audience: JWT audience claim. When non-empty, encoded into the
            token and validated on decode. Matches the original
            ``GUEST_TOKEN_JWT_AUDIENCE`` config behaviour.

    Returns:
        Encoded JWT token string.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "user": user,
        "resources": resources,
        "rls_rules": rls,
        "type": _GUEST_TOKEN_TYPE,
        "iat": now,
        "exp": now + exp_seconds,
        "aud": audience,  # always encode aud; matches superset_old/security/manager.py:2717
    }
    return jwt.encode(payload, secret_key, algorithm=_GUEST_TOKEN_ALGORITHM)


def parse_guest_token(
    token: str,
    secret_key: str,
    algorithm: str = _GUEST_TOKEN_ALGORITHM,
    audience: str = "",
) -> dict[str, Any] | None:
    """Parse and validate a guest JWT token.

    Args:
        token: Raw JWT token string.
        secret_key: Application secret key for validation.
        algorithm: JWT algorithm (default: HS256).
        audience: Expected JWT audience claim. When non-empty, PyJWT
            validates the ``aud`` claim matches. Mirrors the original
            ``GUEST_TOKEN_JWT_AUDIENCE`` behaviour.

    Returns:
        Decoded payload dict if valid, None otherwise.
    """
    decode_kwargs: dict[str, Any] = {
        "algorithms": [algorithm],
    }
    if audience:
        decode_kwargs["audience"] = audience
    try:
        payload = jwt.decode(
            token,
            secret_key,
            **decode_kwargs,
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


def validate_guest_token_resources_schema(
    resources: list[dict[str, Any]],
) -> list[str]:
    """Validate guest token resource entries (schema-level only).

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


async def validate_guest_token_resources(
    resources: list[dict[str, Any]],
    session: Any,
) -> list[str]:
    """Validate guest token resources: schema + DB existence checks.

    Matches the original SupersetSecurityManager.validate_guest_token_resources:
    1. Schema validation (type, id fields present and supported).
    2. For dashboard resources, verify the dashboard exists in the DB.
       First checks Dashboard by ID/slug, then checks EmbeddedDashboard
       by UUID. If neither found, reports an error.

    Args:
        resources: List of resource dicts from the guest token payload.
        session: AsyncSession for database lookups.

    Returns:
        List of validation error messages. Empty list means all valid.
    """
    # Phase 1: schema validation
    errors = validate_guest_token_resources_schema(resources)
    if errors:
        return errors

    # Phase 2: DB existence checks for dashboard resources
    from superset.db.daos.dashboard import (
        AsyncDashboardDAO,
        AsyncEmbeddedDashboardDAO,
    )

    dashboard_dao = AsyncDashboardDAO(session)
    embedded_dao = AsyncEmbeddedDashboardDAO(session)

    for i, resource in enumerate(resources):
        if resource.get("type") == "dashboard":
            resource_id = str(resource["id"])

            # Check 1: Dashboard.get(str(resource["id"]))
            # Uses get_by_id_or_slug which tries int ID, UUID, then slug
            dashboard = await dashboard_dao.get_by_id_or_slug(resource_id)
            if not dashboard:
                # Check 2: EmbeddedDashboardDAO.find_by_id(str(resource["id"]))
                # The original uses id_column_name = "uuid", so find_by_id
                # looks up by UUID
                embedded = await embedded_dao.find_by_uuid(resource_id)
                if not embedded:
                    errors.append(
                        f"Resource {i}: embedded dashboard not found "
                        f"for id '{resource_id}'"
                    )
    return errors
