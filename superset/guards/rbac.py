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


def has_permissions(user: Any, required: set[tuple[str, str]]) -> bool:
    user_perms: set[tuple[str, str]] = getattr(user, "permissions", set())
    return required.issubset(user_perms)


def require_authentication(
    connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler
) -> None:
    """Guard that requires authentication OR Public role permissions.

    Allows anonymous users if they have been granted permissions
    via the Public role (``auth_role_public`` config). Rejects
    anonymous users with no permissions at all.
    """
    user = connection.user
    if getattr(user, "is_authenticated", False):
        return
    # Allow anonymous users with Public role permissions
    permissions: set[tuple[str, str]] = getattr(user, "permissions", set())
    if permissions:
        return
    raise NotAuthorizedException(detail="Not authenticated")


def require_authenticated_user(
    connection: ASGIConnection[Any, Any, Any, Any],
    _: BaseRouteHandler,
) -> None:
    """Guard that strictly requires is_authenticated=True.

    Rejects anonymous users even with Public role permissions.
    """
    user = connection.user
    if not getattr(user, "is_authenticated", False):
        raise NotAuthorizedException(
            detail="Not authenticated",
        )


def deny_anon_with_403(
    connection: ASGIConnection[Any, Any, Any, Any],
    _: BaseRouteHandler,
) -> None:
    """Guard that rejects anonymous users with 403 instead of 401.

    Used for endpoints (like ``/explore/permalink`` POST) where the
    original Superset deployment returns 403 to unauthenticated callers
    rather than 401.
    """
    user = connection.user
    if not getattr(user, "is_authenticated", False):
        raise PermissionDeniedException(detail="Forbidden")


def deny_anon_with_404(
    connection: ASGIConnection[Any, Any, Any, Any],
    _: BaseRouteHandler,
) -> None:
    """Guard that hides the route from anonymous users with 404.

    Mirrors deployments where unauthenticated callers see "not found"
    for sub-resource routes (e.g. ``/dashboard/{pk}/permalink``,
    ``/dashboard/{pk}/embedded``) — the dashboard itself is filtered
    out of the user's view and the sub-route appears non-existent.
    """
    from litestar.exceptions import NotFoundException

    user = connection.user
    if not getattr(user, "is_authenticated", False):
        raise NotFoundException(detail="Not found")


def require_permission(action: str, resource: str) -> GuardFn:
    permission_tuple = (action, resource)

    def guard_fn(
        connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler
    ) -> None:
        user = connection.user
        # Check if user is authenticated
        if not getattr(user, "is_authenticated", False):
            # Allow anonymous users with matching Public role permission
            user_perms: set[tuple[str, str]] = getattr(user, "permissions", set())
            if permission_tuple in user_perms:
                return
            raise NotAuthorizedException(detail="Not authenticated")
        if is_admin(user):
            return
        if not has_permissions(user, {permission_tuple}):
            raise PermissionDeniedException(
                detail=f"Missing permission: {action} on {resource}"
            )

    return guard_fn
