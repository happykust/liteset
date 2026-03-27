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
"""Current user controller — /api/v1/me/ endpoints."""

from __future__ import annotations

from typing import Any

import msgspec
from litestar import Controller, get, put
from litestar.di import Provide

from liteset.events import event_logger
from liteset.guards.rbac import require_authentication
from liteset.schemas.user import (
    CurrentUserResponse,
    CurrentUserUpdateRequest,
    RoleResponseSchema,
)
from liteset.typing import UserProtocol


def _provide_user_dao(session: Any) -> Any:
    """Lazy provider for AsyncUserDAO — avoids eager Flask imports."""
    from liteset.db.daos.user import AsyncUserDAO

    return AsyncUserDAO(session)


class CurrentUserController(Controller):
    path = "/api/v1/me"
    tags = ["Current User"]
    dependencies = {
        "user_dao": Provide(_provide_user_dao, sync_to_thread=False),
    }

    @get("/", guards=[require_authentication])
    async def get_me(self, current_user: UserProtocol) -> dict[str, Any]:
        """GET /api/v1/me/ — get current user info."""
        roles = []
        user_roles = getattr(current_user, "roles", [])
        for role in user_roles:
            roles.append(
                RoleResponseSchema(
                    id=getattr(role, "id", 0),
                    name=getattr(role, "name", ""),
                )
            )

        resp = CurrentUserResponse(
            id=current_user.id,
            username=current_user.username,
            first_name=getattr(current_user, "first_name", ""),
            last_name=getattr(current_user, "last_name", ""),
            email=getattr(current_user, "email", ""),
            is_active=getattr(current_user, "is_active", True),
            is_anonymous=not current_user.is_authenticated,
            roles=roles,
        )
        return {"result": msgspec.to_builtins(resp)}

    @get("/roles/", guards=[require_authentication])
    async def get_my_roles(self, current_user: UserProtocol) -> dict[str, Any]:
        """GET /api/v1/me/roles/ — get current user roles."""
        roles = []
        user_roles = getattr(current_user, "roles", [])
        for role in user_roles:
            roles.append(
                {
                    "id": getattr(role, "id", 0),
                    "name": getattr(role, "name", ""),
                }
            )
        return {"result": roles}

    @put("/", guards=[require_authentication])
    async def update_me(
        self,
        data: CurrentUserUpdateRequest,
        current_user: UserProtocol,
        user_dao: Any,
    ) -> dict[str, Any]:
        """PUT /api/v1/me/ — update current user (first_name, last_name)."""
        updates: dict[str, str] = {}
        if data.first_name is not None:
            updates["first_name"] = data.first_name
        if data.last_name is not None:
            updates["last_name"] = data.last_name

        if updates:
            user = await user_dao.get_by_id(current_user.id)
            if user is not None:
                for attr, value in updates.items():
                    setattr(user, attr, value)
                await user_dao.session.flush()

        event_logger.log("user.update_me", user_id=current_user.id)
        return {"result": updates}
