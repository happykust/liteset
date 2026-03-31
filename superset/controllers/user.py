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
"""User controllers — full CRUD for FAB users (ab_user table)."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.di import Provide
from litestar.response import Redirect

from superset.controllers.base import extract_pagination
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError, SupersetValidationException
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_user_crud_dao
from superset.schemas.security import (
    UserGroupRef,
    UserResponse,
    UserRoleRef,
    UsersSearchResponse,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class UserPostBody(msgspec.Struct):
    """POST body for creating a user."""

    first_name: str
    last_name: str
    username: str
    email: str
    password: str
    active: bool = True
    roles: list[int] = []
    groups: list[int] = []


class UserPutBody(msgspec.Struct):
    """PUT body for updating a user."""

    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    email: str | None = None
    password: str | None = None
    active: bool | None = None
    roles: list[int] | None = None
    groups: list[int] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_to_response(user: Any) -> UserResponse:
    """Convert a User model instance to a UserResponse schema."""
    roles = [
        UserRoleRef(id=r.id, name=r.name)
        for r in (getattr(user, "roles", None) or [])
    ]
    groups = [
        UserGroupRef(id=g.id, name=g.name)
        for g in (getattr(user, "groups", None) or [])
    ]
    return UserResponse(
        id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
        email=user.email,
        active=user.active if user.active is not None else True,
        roles=roles,
        groups=groups,
        login_count=user.login_count,
        fail_login_count=user.fail_login_count,
        last_login=user.last_login.isoformat() if user.last_login else None,
        created_on=user.created_on.isoformat() if user.created_on else None,
        changed_on=user.changed_on.isoformat() if user.changed_on else None,
    )


def _hash_password(password: str) -> str:
    """Hash a password (werkzeug-compatible, no werkzeug dependency)."""
    from superset.utils.password import generate_password_hash

    return generate_password_hash(password)


def _build_user_filters(rison_params: dict[str, Any] | None) -> list[Any]:
    """Build SQLAlchemy filter expressions from Rison filter list."""
    from superset.models.security import User
    from superset.utils import escape_like

    filters: list[Any] = []
    if not rison_params:
        return filters

    allowed = {
        "username": User.username,
        "first_name": User.first_name,
        "last_name": User.last_name,
        "email": User.email,
        "active": User.active,
    }

    for f in rison_params.get("filters", []):
        col_name = f.get("col")
        opr = f.get("opr", "eq")
        value = f.get("value")
        col = allowed.get(col_name)
        if col is None:
            continue

        if opr == "eq":
            filters.append(col == value)
        elif opr == "neq":
            filters.append(col != value)
        elif opr == "sw":
            filters.append(col.ilike(f"{escape_like(str(value))}%"))
        elif opr == "ct":
            filters.append(col.ilike(f"%{escape_like(str(value))}%"))

    return filters


# ---------------------------------------------------------------------------
# User CRUD Controller
# ---------------------------------------------------------------------------


class UserController(Controller):
    """Full CRUD controller for FAB users."""

    path = "/api/v1/security/users"
    tags = ["Security Users"]
    dependencies = {
        "user_dao": Provide(provide_user_crud_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET / — list users (paginated)
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "User")],
    )
    async def get_list(
        self,
        user_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/users/ — list users with Rison pagination."""
        params = rison_params or {}
        page, page_size = extract_pagination(rison_params)
        order_column = params.get("order_column", "id")
        order_direction = params.get("order_direction", "asc")

        if order_column not in (
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "active",
            "last_login",
            "created_on",
            "changed_on",
        ):
            order_column = "id"

        filters = _build_user_filters(rison_params)

        users, total = await user_dao.search(
            filters=filters,
            order_column=order_column,
            order_direction=order_direction,
            page=page,
            page_size=page_size,
        )

        result = [_user_to_response(u) for u in users]
        event_logger.log("user.list")
        return msgspec.to_builtins(
            UsersSearchResponse(result=result, count=total)
        )

    # ------------------------------------------------------------------
    # GET /{pk} — single user
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "User")],
    )
    async def get_single(
        self,
        user_dao: Any,
        pk: int,
    ) -> dict[str, Any]:
        """GET /api/v1/security/users/{pk} — get a single user by ID."""
        user = await user_dao.find_by_id(pk)
        if user is None:
            raise ObjectNotFoundError("User", pk)

        result = _user_to_response(user)
        event_logger.log("user.show", object_ref=str(pk))
        return {"id": pk, "result": msgspec.to_builtins(result)}

    # ------------------------------------------------------------------
    # POST / — create user
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "User")],
        status_code=201,
    )
    async def create(
        self,
        user_dao: Any,
        data: UserPostBody,
    ) -> dict[str, Any]:
        """POST /api/v1/security/users/ — create a new user."""
        if not data.username or not data.username.strip():
            raise SupersetValidationException("Username is required")
        if not data.email or not data.email.strip():
            raise SupersetValidationException("Email is required")
        if not data.password:
            raise SupersetValidationException("Password is required")

        attrs: dict[str, Any] = {
            "first_name": data.first_name.strip(),
            "last_name": data.last_name.strip(),
            "username": data.username.strip(),
            "email": data.email.strip(),
            "password": _hash_password(data.password),
            "active": data.active,
            "created_on": datetime.now(),
            "changed_on": datetime.now(),
            "role_ids": data.roles,
            "group_ids": data.groups,
        }

        user = await user_dao.create(attrs)
        event_logger.log("user.create", extra={"username": data.username})
        return {"id": user.id, "result": msgspec.to_builtins(_user_to_response(user))}

    # ------------------------------------------------------------------
    # PUT /{pk} — update user
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "User")],
    )
    async def update(
        self,
        user_dao: Any,
        pk: int,
        data: UserPutBody,
    ) -> dict[str, Any]:
        """PUT /api/v1/security/users/{pk} — update a user."""
        user = await user_dao.find_by_id(pk)
        if user is None:
            raise ObjectNotFoundError("User", pk)

        attrs: dict[str, Any] = {"changed_on": datetime.now()}
        if data.first_name is not None:
            attrs["first_name"] = data.first_name.strip()
        if data.last_name is not None:
            attrs["last_name"] = data.last_name.strip()
        if data.username is not None:
            attrs["username"] = data.username.strip()
        if data.email is not None:
            attrs["email"] = data.email.strip()
        if data.password is not None and data.password:
            attrs["password"] = _hash_password(data.password)
        if data.active is not None:
            attrs["active"] = data.active
        if data.roles is not None:
            attrs["role_ids"] = data.roles
        if data.groups is not None:
            attrs["group_ids"] = data.groups

        updated = await user_dao.update(user, attrs)
        event_logger.log("user.update", object_ref=str(pk))
        return {
            "id": pk,
            "result": msgspec.to_builtins(_user_to_response(updated)),
        }

    # ------------------------------------------------------------------
    # DELETE /{pk} — delete user
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "User")],
        status_code=200,
    )
    async def delete_single(
        self,
        user_dao: Any,
        pk: int,
    ) -> dict[str, str]:
        """DELETE /api/v1/security/users/{pk} — delete a user."""
        user = await user_dao.find_by_id(pk)
        if user is None:
            raise ObjectNotFoundError("User", pk)

        await user_dao.delete(user)
        event_logger.log("user.delete", object_ref=str(pk))
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET /_info — metadata for frontend forms
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_read", "User")],
    )
    async def get_info(self) -> dict[str, Any]:
        """GET /api/v1/security/users/_info — permissions, columns metadata."""
        permissions = ["can_read", "can_write"]
        add_columns = [
            "first_name",
            "last_name",
            "username",
            "email",
            "password",
            "active",
            "roles",
            "groups",
        ]
        edit_columns = add_columns
        return {
            "permissions": permissions,
            "add_columns": add_columns,
            "edit_columns": edit_columns,
        }

    # ------------------------------------------------------------------
    # GET /{pk}/avatar.png — avatar redirect (preserved from original)
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/avatar.png",
        opt={"exclude_from_auth": True},
    )
    async def get_avatar(self, user_dao: Any, pk: int) -> Redirect:
        """GET /api/v1/security/users/{pk}/avatar.png — redirect to avatar URL."""
        user = await user_dao.find_by_id(pk)
        if user is None:
            raise ObjectNotFoundError("User", pk)

        avatar_url = None
        extra_attrs = getattr(user, "extra_attributes", [])
        if extra_attrs:
            first_attr = (
                extra_attrs[0] if isinstance(extra_attrs, list) else extra_attrs
            )
            avatar_url = getattr(first_attr, "avatar_url", None)

        if not avatar_url:
            email = getattr(user, "email", "") or ""
            email_hash = hashlib.md5(  # noqa: S324
                email.lower().strip().encode()
            ).hexdigest()
            avatar_url = f"https://www.gravatar.com/avatar/{email_hash}?d=mm"

        return Redirect(path=avatar_url)


class UserRegistrationsController(Controller):
    path = "/api/v1/security/user_registrations"
    tags = ["User Registrations"]

    @get(
        "/",
        guards=[require_permission("can_read", "UserRegistrations")],
    )
    async def get_list(self, **kwargs: Any) -> dict[str, Any]:
        """GET /api/v1/security/user_registrations/ — list pending registrations."""
        return {"result": [], "count": 0}

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "UserRegistrations")],
    )
    async def get_single(self, pk: int, **kwargs: Any) -> dict[str, Any]:
        """GET /api/v1/security/user_registrations/{pk} — get single registration."""
        raise ObjectNotFoundError("UserRegistration", pk)
