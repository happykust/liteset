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
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException
from litestar.handlers import BaseRouteHandler

GuardFn = Callable[[ASGIConnection[Any, Any, Any, Any], BaseRouteHandler], None]


def is_admin(user: Any, admin_role_name: str = "Admin") -> bool:
    """Check if user has the Admin role (bypasses all permission checks)."""
    roles = getattr(user, "roles", [])
    return any(getattr(r, "name", None) == admin_role_name for r in roles)


def has_permissions(user: Any, required: set[str]) -> bool:
    user_perms: set[str] = getattr(user, "permissions", set())
    return required.issubset(user_perms)


def require_authentication(
    connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler
) -> None:
    """Guard that only requires the user to be
    authenticated (no specific permission).
    """
    user = connection.user
    if not getattr(user, "is_authenticated", False):
        raise NotAuthorizedException(detail="Not authenticated")


def require_permission(action: str, resource: str) -> GuardFn:
    permission_name = f"{action}_{resource}"

    def guard_fn(
        connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler
    ) -> None:
        user = connection.user
        if not getattr(user, "is_authenticated", False):
            raise NotAuthorizedException(detail="Not authenticated")
        if is_admin(user):
            return
        if not has_permissions(user, {permission_name}):
            raise PermissionDeniedException(
                detail=f"Missing permission: {permission_name}"
            )

    return guard_fn
