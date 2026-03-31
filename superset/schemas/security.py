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
"""Security-related response schemas for superset."""

from __future__ import annotations

import msgspec


# ---------------------------------------------------------------------------
# Role schemas
# ---------------------------------------------------------------------------


class RoleResponse(msgspec.Struct):
    """Single role in search results."""

    id: int
    name: str
    user_ids: list[int] = []
    permission_ids: list[int] = []
    group_ids: list[int] = []


class RolesSearchResponse(msgspec.Struct):
    """Response for GET /api/v1/security/roles/search/."""

    result: list[RoleResponse] = []
    count: int = 0
    ids: list[int] = []


# ---------------------------------------------------------------------------
# User schemas
# ---------------------------------------------------------------------------


class UserRoleRef(msgspec.Struct):
    """Nested role reference in user responses."""

    id: int
    name: str


class UserGroupRef(msgspec.Struct):
    """Nested group reference in user responses."""

    id: int
    name: str


class UserResponse(msgspec.Struct):
    """Single user in search/show results (matches FAB list_columns)."""

    id: int
    first_name: str
    last_name: str
    username: str
    email: str
    active: bool = True
    roles: list[UserRoleRef] = []
    groups: list[UserGroupRef] = []
    login_count: int | None = None
    fail_login_count: int | None = None
    last_login: str | None = None
    created_on: str | None = None
    changed_on: str | None = None


class UsersSearchResponse(msgspec.Struct):
    """Response for GET /api/v1/security/users/."""

    result: list[UserResponse] = []
    count: int = 0


# ---------------------------------------------------------------------------
# Group schemas
# ---------------------------------------------------------------------------


class GroupRoleRef(msgspec.Struct):
    """Nested role reference in group responses."""

    id: int
    name: str


class GroupUserRef(msgspec.Struct):
    """Nested user reference in group responses."""

    id: int
    username: str


class GroupResponse(msgspec.Struct):
    """Single group in search/show results."""

    id: int
    name: str
    label: str | None = None
    description: str | None = None
    roles: list[GroupRoleRef] = []
    users: list[GroupUserRef] = []


class GroupsSearchResponse(msgspec.Struct):
    """Response for GET /api/v1/security/groups/."""

    result: list[GroupResponse] = []
    count: int = 0


# ---------------------------------------------------------------------------
# PermissionView schemas
# ---------------------------------------------------------------------------


class PermissionRef(msgspec.Struct):
    """Nested permission reference."""

    name: str


class ViewMenuRef(msgspec.Struct):
    """Nested view_menu reference."""

    name: str


class PermissionViewResponse(msgspec.Struct):
    """Single permission-view in list results."""

    id: int
    permission: PermissionRef | None = None
    view_menu: ViewMenuRef | None = None


class PermissionViewsSearchResponse(msgspec.Struct):
    """Response for GET /api/v1/security/permissions-resources/."""

    result: list[PermissionViewResponse] = []
    count: int = 0
