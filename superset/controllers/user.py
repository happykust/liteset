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
"""User controllers — full CRUD for upstream users (ab_user table)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import msgspec
from litestar import Controller, delete, get, post, put, Request
from litestar.datastructures import State
from litestar.di import Provide
from litestar.exceptions import HTTPException
from litestar.response import Redirect

from superset.controllers.base import extract_pagination
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError, SupersetValidationException
from superset.guards.rbac import require_authentication, require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_register_user_dao, provide_user_crud_dao
from superset.schemas.security import (
    UserAuditRef,
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
        # Upstream serialises only ``created_by.id``/``changed_by.id`` — build the
        # refs from the FK columns directly (no lazy relationship access).
        created_by=(
            UserAuditRef(id=user.created_by_fk)
            if getattr(user, "created_by_fk", None)
            else None
        ),
        changed_by=(
            UserAuditRef(id=user.changed_by_fk)
            if getattr(user, "changed_by_fk", None)
            else None
        ),
    )


def _check_password_complexity(password: str, settings: Any | None) -> None:
    """Raise HTTPException(400) if the password fails the complexity rules.

    Mirrors the upstream PasswordComplexityValidator.__call__
    and the equivalent check in user_me.py:224-239.
    """
    if settings is None:
        return
    if not getattr(settings, "fab_password_complexity_enabled", False):
        return
    from superset.utils.password import default_password_complexity

    validator = getattr(settings, "fab_password_complexity_validator", None)
    try:
        if validator is not None:
            validator(password)
        else:
            default_password_complexity(password)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"password": [str(exc)]},  # type: ignore[arg-type]
        ) from exc


async def _validate_entity_ids(
    session: Any,
    requested_ids: list[int],
    model_name: str,
    field_name: str,
    status_code: int = 400,
) -> None:
    """Raise HTTPException when any requested ID does not exist in the DB.

    Mirrors the upstream UserApi behavior:
    - POST (post()): response_400 → status_code=400 (default)
    - PUT  (put()):  response_404 → status_code=404

    UserApi.post() lines 132-159 use response_400(...).
    UserApi.put() lines 249-276 use response_404(...) for the same check.
    """
    if not requested_ids:
        return
    from sqlalchemy import select

    from superset.models.security import Group, Role

    _EntityModel: type[Any] = Role if field_name == "roles" else Group  # noqa: N806

    stmt = select(_EntityModel.id).where(_EntityModel.id.in_(requested_ids))
    result = await session.execute(stmt)
    found_ids = {row[0] for row in result}
    missing_ids = sorted(set(requested_ids) - found_ids)
    if missing_ids:
        raise HTTPException(
            status_code=status_code,
            detail={  # type: ignore[arg-type]
                field_name: [f"{model_name}(s) with ID(s) {missing_ids} not found."]
            },
        )


def _hash_password(password: str, settings: Any | None = None) -> str:
    """Hash a password using FAB_PASSWORD_HASH_METHOD and FAB_PASSWORD_HASH_SALT_LENGTH.

    Mirrors pre_update (superset_old/views/users/api.py:52-56) which reads
    FAB_PASSWORD_HASH_METHOD (default 'scrypt') and
    FAB_PASSWORD_HASH_SALT_LENGTH (default 16) from app.config and passes
    them to the secure-hash helper generate_password_hash.
    """
    from superset.utils.password import generate_password_hash

    method = "scrypt"
    salt_length = 16
    if settings is not None:
        method = getattr(settings, "fab_password_hash_method", "scrypt") or "scrypt"
        salt_length = getattr(settings, "fab_password_hash_salt_length", 16) or 16

    return generate_password_hash(password, method=method, salt_length=salt_length)


def _apply_simple_filter(
    col: Any, opr: str, value: Any, escape_like: Any
) -> Any | None:
    """Return a single SQLAlchemy filter expression for a scalar column.

    Returns None when the operator is unrecognised (caller skips it).
    """
    if opr == "eq":
        return col == value
    if opr == "neq":
        return col != value
    if opr == "sw":
        return col.ilike(f"{escape_like(str(value))}%")
    if opr == "ct":
        return col.ilike(f"%{escape_like(str(value))}%")
    if opr == "gt":
        return col > value
    if opr == "lt":
        return col < value
    if opr == "gte":
        return col >= value
    if opr == "lte":
        return col <= value
    return None


def _validate_user_update_payload(
    item_roles: list[int] | None,
    item_groups: list[int] | None,
    user: Any,
) -> None:
    """Guard against clearing a user's last role/group assignment.

    Mirrors the upstream UserApi.put() lines 225-244 which return HTTP 400 in
    three cases:
    1. Both roles and groups are explicitly cleared to [].
    2. Roles are cleared to [] and the user has no existing groups (and none
       are being assigned).
    3. Groups are cleared to [] and the user has no existing roles (and none
       are being assigned).

    Raises:
        HTTPException: 400 when any guard condition is met.
    """
    if item_roles == [] and item_groups == []:
        raise HTTPException(
            status_code=400,
            detail="User must have at least one role or group!",
        )

    if item_roles == [] and (item_groups is None and not user.groups):
        raise HTTPException(
            status_code=400,
            detail=("Cannot clear all roles unless at least one group is assigned!"),
        )

    if item_groups == [] and (item_roles is None and not user.roles):
        raise HTTPException(
            status_code=400,
            detail=("Cannot clear all groups unless at least one role is assigned!"),
        )


async def _validate_user_update_extended(
    data: "UserPutBody",
    session: Any,
    settings: Any | None,
) -> None:
    """Run async/settings-dependent validation for a user PUT request.

    Mirrors the upstream UserApi.put() lines 245-283:
    - PasswordComplexityValidator on the new password (if provided).
    - Role/group ID existence checks (HTTP 404 if any ID is missing).

    Note: the upstream UserApi.put() uses response_404() for missing role/group
    IDs (lines 252-276), unlike post() which uses response_400() (lines
    135-158).

    Extracted from ``update`` to keep that handler's cyclomatic complexity
    within the C901 threshold.
    """
    if data.password:
        _check_password_complexity(data.password, settings)
    if data.roles is not None:
        await _validate_entity_ids(
            session, data.roles, "Role", "roles", status_code=404
        )
    if data.groups is not None:
        await _validate_entity_ids(
            session, data.groups, "Group", "groups", status_code=404
        )


def _build_user_filters(rison_params: dict[str, Any] | None) -> list[Any]:
    """Build SQLAlchemy filter expressions from Rison filter list.

    Mirrors SupersetUserApi.search_columns (superset_old/security/manager.py:150-164)
    which includes: id, roles, groups, first_name, last_name, username, active,
    email, last_login, login_count, fail_login_count, created_on, changed_on.
    """
    from superset.models.security import User
    from superset.utils import escape_like

    filters: list[Any] = []
    if not rison_params:
        return filters

    # Simple column filters (equality, inequality, substring)
    simple_cols: dict[str, Any] = {
        "id": User.id,
        "username": User.username,
        "first_name": User.first_name,
        "last_name": User.last_name,
        "email": User.email,
        "active": User.active,
        "last_login": User.last_login,
        "login_count": User.login_count,
        "fail_login_count": User.fail_login_count,
        "created_on": User.created_on,
        "changed_on": User.changed_on,
    }

    for f in rison_params.get("filters", []):
        col_name = f.get("col")
        opr = f.get("opr", "eq")
        value = f.get("value")

        # Relationship filters (roles, groups) — mirrors original .any(id=value)
        if col_name == "roles":
            filters.append(User.roles.any(id=value))
            continue
        if col_name == "groups":
            filters.append(User.groups.any(id=value))
            continue

        col = simple_cols.get(col_name)
        if col is None:
            continue

        expr = _apply_simple_filter(col, opr, value, escape_like)
        if expr is not None:
            filters.append(expr)

    return filters


# ---------------------------------------------------------------------------
# User CRUD Controller
# ---------------------------------------------------------------------------


class UserController(Controller):
    """Full CRUD controller for upstream users."""

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
            # Upstream list_columns include both counters → orderable upstream.
            "login_count",
            "fail_login_count",
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
        ids = [u.id for u in users]
        await event_logger.alog_with_context("user.list")
        return msgspec.to_builtins(
            UsersSearchResponse(result=result, count=total, ids=ids)
        )

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
        state: State,
        request: Request[Any, Any, Any],
    ) -> dict[str, Any]:
        """POST /api/v1/security/users/ — create a new user."""
        if not data.username or not data.username.strip():
            raise SupersetValidationException("Username is required")
        if not data.email or not data.email.strip():
            raise SupersetValidationException("Email is required")
        if not data.password:
            raise SupersetValidationException("Password is required")

        # The upstream UserPostSchema.validate_roles_or_groups_present():
        # raises ValidationError (→ response_400) when both roles and groups
        # are empty or absent.  Mirror that guard here (HTTP 400).
        if not data.roles and not data.groups:
            raise HTTPException(
                status_code=400,
                detail=(
                    "At least one of 'roles' or 'groups' must be provided"
                    " and non-empty."
                ),
            )

        settings = getattr(state, "settings", None)

        # Upstream UserPostSchema: validate=[PasswordComplexityValidator()] on pwd
        _check_password_complexity(data.password, settings)

        # Upstream UserApi.post() lines 132-159: verify role/group IDs exist → 400
        await _validate_entity_ids(user_dao.session, data.roles, "Role", "roles")
        await _validate_entity_ids(user_dao.session, data.groups, "Group", "groups")

        attrs: dict[str, Any] = {
            "first_name": data.first_name.strip(),
            "last_name": data.last_name.strip(),
            "username": data.username.strip(),
            "email": data.email.strip(),
            "password": _hash_password(data.password, settings=settings),
            "active": data.active,
            "created_on": datetime.now(),
            "changed_on": datetime.now(),
            "role_ids": data.roles,
            "group_ids": data.groups,
        }
        current_user = getattr(request, "user", None)
        if current_user and getattr(current_user, "id", None):
            attrs["created_by_fk"] = current_user.id
            attrs["changed_by_fk"] = current_user.id

        user = await user_dao.create(attrs)
        await event_logger.alog_with_context(
            "user.create", extra={"username": data.username}
        )
        # 1:1 with the upstream UserApi.post():
        # ``self.response(201, id=model.id)`` — no ``result`` key.
        return {"id": user.id}

    # ------------------------------------------------------------------
    # PUT /{pk} — update user
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_put", "User")],
    )
    async def update(  # noqa: C901
        self,
        user_dao: Any,
        pk: int,
        data: UserPutBody,
        state: State,
        request: Request[Any, Any, Any],
    ) -> dict[str, Any]:
        """PUT /api/v1/security/users/{pk} — update a user."""
        user = await user_dao.find_by_id(pk)
        if user is None:
            raise ObjectNotFoundError("User", pk)

        # Upstream UserApi.put() lines 225-244: three guard blocks that prevent
        # clearing a user's last role/group, returning HTTP 400.
        _validate_user_update_payload(data.roles, data.groups, user)

        settings = getattr(state, "settings", None)
        # Upstream UserPutSchema: password complexity + role/group ID checks (400)
        await _validate_user_update_extended(data, user_dao.session, settings)

        attrs: dict[str, Any] = {"changed_on": datetime.now()}
        current_user = getattr(request, "user", None)
        if current_user and getattr(current_user, "id", None):
            attrs["changed_by_fk"] = current_user.id
        if data.first_name is not None:
            attrs["first_name"] = data.first_name.strip()
        if data.last_name is not None:
            attrs["last_name"] = data.last_name.strip()
        if data.username is not None:
            attrs["username"] = data.username.strip()
        if data.email is not None:
            attrs["email"] = data.email.strip()
        if data.password is not None and data.password:
            attrs["password"] = _hash_password(data.password, settings=settings)
        if data.active is not None:
            attrs["active"] = data.active
        if data.roles is not None:
            attrs["role_ids"] = data.roles
        if data.groups is not None:
            attrs["group_ids"] = data.groups

        await user_dao.update(user, attrs)
        await event_logger.alog_with_context("user.update", object_ref=str(pk))

        # 1:1 with the upstream UserApi.put() return: echoes the provided fields
        # without `id`.
        result_dict = {
            k: getattr(data, k)
            for k in data.__struct_fields__
            if getattr(data, k) is not None
        }
        return {"result": result_dict}

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

    Mirrors the upstream auto-generated ``add_model_schema`` for RegisterUser.
    All non-PK model columns are accepted. ``registration_date`` and
    ``registration_hash`` are optional (nullable in the model).
    Column order follows the upstream RegisterUser model definition:
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
    Mirrors the upstream auto-generated ``edit_model_schema`` for RegisterUser.
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


def _reg_to_dict(reg: Any, is_list: bool = False) -> dict[str, Any]:
    """Serialize a RegisterUser model instance to a dict.

    Matches the upstream auto-generated show_columns which includes all non-PK
    columns. The ``id`` is NOT included here because it is returned at
    the top level of the response (``{"id": ..., "result": ...}``),
    avoiding duplication.
    """
    res = {
        "username": reg.username,
        "email": reg.email,
        "first_name": reg.first_name,
        "last_name": reg.last_name,
        "registration_date": (
            reg.registration_date.isoformat() if reg.registration_date else None
        ),
        "registration_hash": reg.registration_hash,
    }
    if not is_list:
        res["password"] = reg.password
    return res


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

    Mirrors the upstream ``ModelRestApi.put_headless`` which merges the request
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
    which extends ``BaseSupersetModelRestApi`` (the upstream ``ModelRestApi``)
    with ``SQLAInterface(RegisterUser)``.

    Original list_columns:
        id, username, email, first_name, last_name, registration_date,
        registration_hash

    The original ``ModelRestApi`` automatically exposes:
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
        Mirrors the upstream ``ModelRestApi.get_list`` behavior.
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

        result = [{"id": r.id, **_reg_to_dict(r, is_list=True)} for r in registrations]
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
        # Upstream auto-populates show_columns from ALL model columns (incl. id),
        # so ``id`` appears inside ``result`` AND at the top level —
        # 88e43b6c2d applied the same to other resources.
        return {"id": pk, "result": {"id": reg.id, **_reg_to_dict(reg)}}

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

        Mirrors the upstream ``ModelRestApi.post_headless``: validates via marshmallow
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

        Mirrors the upstream ``ModelRestApi.put`` for RegisterUser.
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

        Mirrors the upstream ``SecurityManager.del_register_user``.
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

        Mirrors the upstream ``ModelRestApi.info`` endpoint. Returns permissions,
        column metadata for add/edit forms, and available filters.
        """
        permissions = ["can_read", "can_write"]
        # Column order matches the upstream RegisterUser model definition:
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

        Mirrors the upstream ``ModelRestApi.distinct`` endpoint. Returns distinct
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

        Mirrors the upstream ``ModelRestApi.related`` endpoint. RegisterUser has
        no relationship columns, so this always returns an empty result
        set — matching the original behavior where upstream would return empty
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
# This is the public-facing user endpoint, separate from the upstream CRUD
# controller at /api/v1/security/users.
# ---------------------------------------------------------------------------


def _provide_user_dao(session: Any) -> Any:
    """Lazy provider for AsyncUserDAO — avoids eager legacy-stack imports."""
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
        # Require authentication — 1:1 with upstream ``UserRestApi.avatar``
        # whose docstring states it "returns a 401 error if the user is
        # unauthenticated" (superset_old/views/users/api.py:183-205). Avatars
        # load via same-origin ``<img>`` which forwards the session cookie, so
        # authenticated display is unaffected (R15-02).
        guards=[require_authentication],
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
