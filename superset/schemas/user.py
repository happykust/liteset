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
"""msgspec Structs for the User API."""

from __future__ import annotations

import msgspec


class RoleResponseSchema(msgspec.Struct):
    id: int
    name: str


class CurrentUserResponse(msgspec.Struct):
    id: int
    username: str
    first_name: str
    last_name: str
    email: str
    is_active: bool
    is_anonymous: bool
    # ``login_count`` is required and integer in the original
    # ``UserResponseSchema`` (superset_old/views/users/schemas.py:38) — the
    # OpenAPI spec marks it as a non-nullable int, so we default to 0
    # rather than None when the underlying user has never logged in.
    login_count: int = 0
    roles: list[RoleResponseSchema] = []


class CurrentUserUpdateRequest(msgspec.Struct):
    first_name: str | None = None
    last_name: str | None = None
    password: str | None = None
