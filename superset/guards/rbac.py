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

# Default admin role name — overridden by AUTH_ROLE_ADMIN config.  Read
# fresh from ``connection.app.state.settings`` in guards that have access
# to the connection; fall back to this constant only in code paths where
# no connection is available (e.g. unit-test helpers).
_DEFAULT_ADMIN_ROLE_NAME = "Admin"


def is_admin(user: Any, admin_role_name: str = _DEFAULT_ADMIN_ROLE_NAME) -> bool:
    """Check if user has the Admin role (bypasses all permission checks).

    :param user: The user object (must have a ``roles`` attribute).
    :param admin_role_name: The configured admin role name (``AUTH_ROLE_ADMIN``).
        Guards that have access to the Litestar connection should pass
        ``connection.app.state.settings.auth_role_admin`` so that deployments
        with a custom admin role value are always handled correctly.
    """
    roles = getattr(user, "roles", [])
    return any(getattr(r, "name", None) == admin_role_name for r in roles)


def has_permissions(user: Any, required: set[tuple[str, str]]) -> bool:
    user_perms: set[tuple[str, str]] = getattr(user, "permissions", set())
    return required.issubset(user_perms)


def require_authentication(
    connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler
) -> None:
    """Guard that requires a fully authenticated session.

    Rejects ALL anonymous / unauthenticated users with HTTP 401 — including
    anonymous users whose Public role carries some permissions — because this
    guard has no knowledge of *which specific* permission the endpoint
    requires.

    ``@protect()`` and ``@has_access_api`` allow anonymous access only when
    the Public role has the *specific* ``(action, resource)`` pair for the
    endpoint (``is_item_public(permission_str, class_permission_name)``).
    A guard that cannot parameterise the required pair must therefore simply
    deny all anonymous callers.

    Endpoints that legitimately allow anonymous Public-role access must use
    ``require_permission(action, resource)`` — that guard does perform the
    correct per-endpoint Public-role check.
    """
    user = connection.user
    if not getattr(user, "is_authenticated", False):
        raise NotAuthorizedException(detail="Not authenticated")


# Alias — both names are imported across controllers but the behaviour is
# (and must stay) identical; a separate copy invited silent divergence.
require_authenticated_user = require_authentication


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


def require_feature_flag(feature: str) -> GuardFn:
    """Guard factory that hides a route with 404 when *feature* is disabled.

    Returns HTTP 404 when ``is_feature_enabled(<feature>)`` is ``False``.

    Applied at the controller level, it gates *every* route on the
    controller.
    """

    def guard_fn(
        connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler
    ) -> None:
        from litestar.exceptions import NotFoundException

        from superset.utils.feature_flags import feature_flag_manager

        if not feature_flag_manager.is_feature_enabled(feature):
            raise NotFoundException(detail="Not found")

    return guard_fn


def require_permission(action: str, resource: str) -> GuardFn:
    permission_tuple = (action, resource)

    def guard_fn(
        connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler
    ) -> None:
        user = connection.user
        # Read admin role name from app state settings (not a stale cached
        # value) so deployments with a custom AUTH_ROLE_ADMIN are respected.
        admin_role = getattr(
            getattr(connection.app.state, "settings", None),
            "auth_role_admin",
            _DEFAULT_ADMIN_ROLE_NAME,
        )
        if not getattr(user, "is_authenticated", False):
            user_perms: set[tuple[str, str]] = getattr(user, "permissions", set())
            if permission_tuple in user_perms:
                return
            raise NotAuthorizedException(detail="Not authenticated")
        if is_admin(user, admin_role_name=admin_role):
            return
        if not has_permissions(user, {permission_tuple}):
            raise PermissionDeniedException(
                detail=f"Missing permission: {action} on {resource}"
            )

    return guard_fn
