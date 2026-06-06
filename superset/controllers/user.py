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

import logging
from datetime import datetime
from typing import Any

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.datastructures import State
from litestar.di import Provide
from litestar.response import Redirect

from superset.controllers.base import extract_pagination
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError, SupersetValidationException
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_register_user_dao, provide_user_crud_dao
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
        UserRoleRef(id=r.id, name=r.name) for r in (getattr(user, "roles", None) or [])
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

    allowed: dict[str | None, Any] = {
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
        guards=[require_permission("can_get", "User")],
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
        await event_logger.alog_with_context("user.list")
        return msgspec.to_builtins(UsersSearchResponse(result=result, count=total))

    # ------------------------------------------------------------------
    # GET /{pk} — single user
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_get", "User")],
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
        await event_logger.alog_with_context("user.show", object_ref=str(pk))
        return {"id": pk, "result": msgspec.to_builtins(result)}

    # ------------------------------------------------------------------
    # POST / — create user
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_post", "User")],
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
        await event_logger.alog_with_context(
            "user.create", extra={"username": data.username}
        )
        return {"id": user.id, "result": msgspec.to_builtins(_user_to_response(user))}

    # ------------------------------------------------------------------
    # PUT /{pk} — update user
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_put", "User")],
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
        await event_logger.alog_with_context("user.update", object_ref=str(pk))
        return {
            "id": pk,
            "result": msgspec.to_builtins(_user_to_response(updated)),
        }

    # ------------------------------------------------------------------
    # DELETE /{pk} — delete user
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_delete", "User")],
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
        await event_logger.alog_with_context("user.delete", object_ref=str(pk))
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET /_info — metadata for frontend forms
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_info", "User")],
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


# ---------------------------------------------------------------------------
# Request / response schemas for user registrations
# ---------------------------------------------------------------------------


class RegisterUserPostBody(msgspec.Struct):
    """POST body for creating a user registration request.

    Mirrors FAB's auto-generated ``add_model_schema`` for RegisterUser.
    All non-PK model columns are accepted. ``registration_date`` and
    ``registration_hash`` are optional (nullable in the model).
    Column order follows FAB's RegisterUser model definition:
    first_name, last_name, username, password, email,
    registration_date, registration_hash.
    """

    first_name: str
    last_name: str
    username: str
    email: str
    password: str | None = None
    registration_date: str | None = None
    registration_hash: str | None = None


class RegisterUserPutBody(msgspec.Struct):
    """PUT body for updating a user registration request.

    All fields are optional — only provided fields are updated.
    Mirrors FAB's auto-generated ``edit_model_schema`` for RegisterUser.
    """

    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    password: str | None = None
    registration_date: str | None = None
    registration_hash: str | None = None


# Columns returned by the list/show endpoints, matching the original
# UserRegistrationsRestAPI.list_columns from superset_old/security/api.py.
_REG_LIST_COLUMNS = [
    "id",
    "username",
    "email",
    "first_name",
    "last_name",
    "registration_date",
    "registration_hash",
]

# Columns allowed for ordering in list requests.
_REG_ORDER_COLUMNS = frozenset(
    {
        "id",
        "username",
        "email",
        "first_name",
        "last_name",
        "registration_date",
    }
)

# Columns allowed for the distinct endpoint.
_REG_DISTINCT_COLUMNS = frozenset(
    {
        "username",
        "email",
        "first_name",
        "last_name",
    }
)


def _reg_to_dict(reg: Any) -> dict[str, Any]:
    """Serialize a RegisterUser model instance to a dict.

    Matches FAB's auto-generated show_columns which includes all non-PK
    columns. The ``id`` is NOT included here because it is returned at
    the top level of the response (``{"id": ..., "result": ...}``),
    avoiding duplication.
    """
    return {
        "username": reg.username,
        "email": reg.email,
        "first_name": reg.first_name,
        "last_name": reg.last_name,
        "password": reg.password,
        "registration_date": (
            reg.registration_date.isoformat() if reg.registration_date else None
        ),
        "registration_hash": reg.registration_hash,
    }


def _build_reg_filters(rison_params: dict[str, Any] | None) -> list[Any]:
    """Build SQLAlchemy filter expressions from Rison filter list for RegisterUser."""
    from superset.models.security import RegisterUser
    from superset.utils import escape_like

    filters: list[Any] = []
    if not rison_params:
        return filters

    allowed: dict[str | None, Any] = {
        "username": RegisterUser.username,
        "email": RegisterUser.email,
        "first_name": RegisterUser.first_name,
        "last_name": RegisterUser.last_name,
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


def _build_reg_update_attrs(data: RegisterUserPutBody) -> dict[str, Any]:
    """Build update attributes dict from PUT body.

    Mirrors FAB's ``ModelRestApi.put_headless`` which merges the request
    JSON into the existing item and persists. Data is stored as received
    — no password hashing (that only happens in the register FORM flow).
    Uniqueness is enforced by the database constraints (unique on
    username and email columns).
    """
    attrs: dict[str, Any] = {}
    if data.first_name is not None:
        attrs["first_name"] = data.first_name
    if data.last_name is not None:
        attrs["last_name"] = data.last_name
    if data.username is not None:
        attrs["username"] = data.username
    if data.email is not None:
        attrs["email"] = data.email
    if data.password is not None:
        attrs["password"] = data.password
    if data.registration_date is not None:
        attrs["registration_date"] = data.registration_date
    if data.registration_hash is not None:
        attrs["registration_hash"] = data.registration_hash
    return attrs


class UserRegistrationsController(Controller):
    """CRUD controller for pending user registrations (ab_register_user).

    Ported 1:1 from ``:UserRegistrationsRestAPI`` in
    ``superset_old/security/api.py``
    which extends ``BaseSupersetModelRestApi`` (FAB's ``ModelRestApi``) with
    ``SQLAInterface(RegisterUser)``.

    Original list_columns:
        id, username, email, first_name, last_name, registration_date,
        registration_hash

    The original FAB ``ModelRestApi`` automatically exposes:
        GET /         (get_list)
        GET /{pk}     (get)
        POST /        (post)
        PUT /{pk}     (put)
        DELETE /{pk}  (delete)
        GET /_info    (info)
        GET /distinct/{column_name} (distinct)
        GET /related/{column_name}  (related)

    Permission mapping from ``BaseSupersetModelRestApi.method_permission_name``:
        get_list, info, distinct, related -> "list" (mapped to can_read)
        get -> "show" (mapped to can_read)
        post -> "add" (mapped to can_write)
        put -> "edit" (mapped to can_write)
        delete -> "delete" (mapped to can_write)
    """

    path = "/api/v1/security/user_registrations"
    tags = ["User Registrations"]
    dependencies = {
        "reg_dao": Provide(provide_register_user_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET / — list pending registrations (paginated)
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "UserRegistrationsRestAPI")],
    )
    async def get_list(
        self,
        reg_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/user_registrations/ — list pending registrations.

        Admin-only, matching the original ``UserRegistrationsRestAPI`` class
        docstring ("Admin only") in ``superset_old/security/api.py:346``.
        The original BaseSupersetModelRestApi maps ``get_list`` to
        ``can_list`` / ``can_read`` on the resource, which is only granted
        to the Admin role by default.

        Supports Rison query parameters for pagination, ordering, and filtering.
        Mirrors FAB's ``ModelRestApi.get_list`` behavior.
        """
        params = rison_params or {}
        page, page_size = extract_pagination(rison_params)
        order_column = params.get("order_column", "id")
        order_direction = params.get("order_direction", "asc")

        if order_column not in _REG_ORDER_COLUMNS:
            order_column = "id"

        filters = _build_reg_filters(rison_params)

        registrations, total = await reg_dao.search(
            filters=filters,
            order_column=order_column,
            order_direction=order_direction,
            page=page,
            page_size=page_size,
        )

        result = [{"id": r.id, **_reg_to_dict(r)} for r in registrations]
        ids = [r.id for r in registrations]
        await event_logger.alog_with_context("user_registrations.list")
        return {"result": result, "ids": ids, "count": total}

    # ------------------------------------------------------------------
    # GET /{pk} — single registration
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "UserRegistrationsRestAPI")],
    )
    async def get_single(
        self,
        reg_dao: Any,
        pk: int,
    ) -> dict[str, Any]:
        """GET /api/v1/security/user_registrations/{pk} — get a single registration."""
        reg = await reg_dao.find_by_id(pk)
        if reg is None:
            raise ObjectNotFoundError("UserRegistration", pk)

        await event_logger.alog_with_context(
            "user_registrations.show", object_ref=str(pk)
        )
        return {"id": pk, "result": _reg_to_dict(reg)}

    # ------------------------------------------------------------------
    # POST / — create a registration request
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "UserRegistrationsRestAPI")],
        status_code=201,
    )
    async def create(
        self,
        reg_dao: Any,
        data: RegisterUserPostBody,
    ) -> dict[str, Any]:
        """POST /api/v1/security/user_registrations/ — create a registration request.

        Mirrors FAB's ``ModelRestApi.post_headless``: validates via marshmallow
        schema (msgspec in our case), then persists via ``session.add() +
        session.flush()``. Data is stored as received — no password hashing
        or ``registration_hash`` generation (those only happen in the
        register FORM flow, not the REST API).
        """
        attrs: dict[str, Any] = {
            "first_name": data.first_name,
            "last_name": data.last_name,
            "username": data.username,
            "email": data.email,
            "password": data.password,
        }
        if data.registration_date is not None:
            attrs["registration_date"] = data.registration_date
        if data.registration_hash is not None:
            attrs["registration_hash"] = data.registration_hash

        reg = await reg_dao.create(attrs)
        await event_logger.alog_with_context(
            "user_registrations.create", extra={"username": data.username}
        )
        return {"id": reg.id, "result": _reg_to_dict(reg)}

    # ------------------------------------------------------------------
    # PUT /{pk} — update a registration request
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "UserRegistrationsRestAPI")],
    )
    async def update(
        self,
        reg_dao: Any,
        pk: int,
        data: RegisterUserPutBody,
    ) -> dict[str, Any]:
        """PUT /api/v1/security/user_registrations/{pk} — update a registration.

        Mirrors FAB's ``ModelRestApi.put`` for RegisterUser.
        Only provided (non-None) fields are updated.
        """
        reg = await reg_dao.find_by_id(pk)
        if reg is None:
            raise ObjectNotFoundError("UserRegistration", pk)

        attrs = _build_reg_update_attrs(data)

        updated = await reg_dao.update(reg, attrs)
        await event_logger.alog_with_context(
            "user_registrations.update", object_ref=str(pk)
        )
        return {"id": pk, "result": _reg_to_dict(updated)}

    # ------------------------------------------------------------------
    # DELETE /{pk} — delete a registration
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "UserRegistrationsRestAPI")],
        status_code=200,
    )
    async def delete_single(
        self,
        reg_dao: Any,
        pk: int,
    ) -> dict[str, str]:
        """DELETE /api/v1/security/user_registrations/{pk} — delete a registration.

        Mirrors FAB's ``SecurityManager.del_register_user``.
        """
        reg = await reg_dao.find_by_id(pk)
        if reg is None:
            raise ObjectNotFoundError("UserRegistration", pk)

        await reg_dao.delete(reg)
        await event_logger.alog_with_context(
            "user_registrations.delete", object_ref=str(pk)
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET /_info — metadata for frontend forms
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_read", "UserRegistrationsRestAPI")],
    )
    async def get_info(self) -> dict[str, Any]:
        """GET /api/v1/security/user_registrations/_info — metadata.

        Mirrors FAB's ``ModelRestApi.info`` endpoint. Returns permissions,
        column metadata for add/edit forms, and available filters.
        """
        permissions = ["can_read", "can_write"]
        # Column order matches FAB's RegisterUser model definition:
        # first_name, last_name, username, password, email,
        # registration_date, registration_hash
        add_columns = [
            "first_name",
            "last_name",
            "username",
            "password",
            "email",
            "registration_date",
            "registration_hash",
        ]
        edit_columns = [
            "first_name",
            "last_name",
            "username",
            "password",
            "email",
            "registration_date",
            "registration_hash",
        ]
        return {
            "permissions": permissions,
            "add_columns": add_columns,
            "edit_columns": edit_columns,
        }

    # ------------------------------------------------------------------
    # GET /distinct/{column_name} — distinct values for a column
    # ------------------------------------------------------------------
    @get(
        "/distinct/{column_name:str}",
        guards=[require_permission("can_read", "UserRegistrationsRestAPI")],
    )
    async def distinct(
        self,
        reg_dao: Any,
        column_name: str,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/user_registrations/distinct/{column_name}.

        Mirrors FAB's ``ModelRestApi.distinct`` endpoint. Returns distinct
        values for the specified column, used by frontend filter UIs.
        """
        if column_name not in _REG_DISTINCT_COLUMNS:
            return {"count": 0, "result": []}

        params = rison_params or {}
        page, page_size = extract_pagination(rison_params)
        filter_value = params.get("filter", "")

        values, total = await reg_dao.get_distinct_values(
            column_name=column_name,
            page=page,
            page_size=page_size,
            filter_value=filter_value,
        )

        return {
            "count": total,
            "result": [{"text": str(v), "value": v} for v in values],
        }

    # ------------------------------------------------------------------
    # GET /related/{column_name} — related values
    # ------------------------------------------------------------------
    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "UserRegistrationsRestAPI")],
    )
    async def related(
        self,
        column_name: str,
    ) -> dict[str, Any]:
        """GET /api/v1/security/user_registrations/related/{column_name}.

        Mirrors FAB's ``ModelRestApi.related`` endpoint. RegisterUser has
        no relationship columns, so this always returns an empty result
        set — matching the original behavior where FAB would return empty
        for models without relationships on the requested column.
        """
        # RegisterUser has no relationship columns (no ForeignKeys to other
        # models), so the related endpoint always returns empty.
        return {"count": 0, "result": []}


# ---------------------------------------------------------------------------
# Public User API — /api/v1/user
# ---------------------------------------------------------------------------
# Ported from superset_old/views/users/api.py ``UserRestApi``
# (resource_name = "user", path = /api/v1/user).
# This is the public-facing user endpoint, separate from the FAB CRUD
# controller at /api/v1/security/users.
# ---------------------------------------------------------------------------


def _provide_user_dao(session: Any) -> Any:
    """Lazy provider for AsyncUserDAO — avoids eager Flask imports."""
    from superset.db.daos.user import AsyncUserDAO

    return AsyncUserDAO(session)


class UserPublicController(Controller):
    """Public user endpoints at /api/v1/user.

    Matches the original Superset ``UserRestApi`` (resource_name = "user")
    which exposed the avatar endpoint at ``/api/v1/user/<user_id>/avatar.png``.
    """

    path = "/api/v1/user"
    tags = ["User"]
    dependencies = {
        "user_dao": Provide(_provide_user_dao, sync_to_thread=False),
    }

    @get(
        "/{user_id:int}/avatar.png",
        opt={"exclude_from_auth": True},
    )
    async def avatar(self, user_dao: Any, user_id: int, state: State) -> Any:
        """GET /api/v1/user/{user_id}/avatar.png — redirect to avatar URL.

        Ported 1:1 from superset_old/views/users/api.py ``UserRestApi.avatar``.

        Checks (in order):
        1. extra_attributes.avatar_url (one-to-one relationship)
        2. Slack API lookup (if SLACK_API_TOKEN configured and
           SLACK_ENABLE_AVATARS feature flag enabled)

        Returns 301 permanent redirect to avatar URL, or 204 if none found.
        """
        from litestar.response import Response
        from sqlalchemy.exc import InvalidRequestError, SQLAlchemyError

        from superset.utils.feature_flags import feature_flag_manager

        user = await user_dao.get_by_id(user_id)
        if user is None:
            # Original returns self.response_404() when user not found
            return Response(status_code=404, content=None)

        avatar_url = None

        # 1. Fetch from the one-to-one relationship
        try:
            await user_dao.session.refresh(user, attribute_names=["extra_attributes"])
        except (InvalidRequestError, SQLAlchemyError):
            pass
        extra_attributes = getattr(user, "extra_attributes", [])
        if extra_attributes and len(extra_attributes) > 0:
            avatar_url = getattr(extra_attributes[0], "avatar_url", None)

        # 2. Try Slack lookup if no avatar and Slack is configured.
        # Read SLACK_API_TOKEN from app config (state.settings), matching
        # the original's ``current_app.config["SLACK_API_TOKEN"]``.
        settings = getattr(state, "settings", None)
        slack_token = getattr(settings, "slack_api_token", None) if settings else None

        if (
            not avatar_url
            and slack_token
            and feature_flag_manager.is_feature_enabled("SLACK_ENABLE_AVATARS")
        ):
            try:
                from superset.utils.slack import get_user_avatar, SlackClientError

                try:
                    avatar_url = get_user_avatar(user.email)
                except SlackClientError:
                    # Original returns self.response_404() on SlackClientError
                    return Response(status_code=404, content=None)

                # Persist so we don't re-fetch next time
                await user_dao.set_avatar_url(user, avatar_url)
            except ImportError:
                pass

        # 3. Return a permanent redirect to the avatar URL (301, matching original)
        if avatar_url:
            return Redirect(path=avatar_url, status_code=301)

        # No avatar found, return a "no-content" response
        return Response(status_code=204, content=None)
