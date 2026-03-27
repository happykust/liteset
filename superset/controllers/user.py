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
"""User controllers — avatar redirect and user registrations."""

from __future__ import annotations

import hashlib
from typing import Any

from litestar import Controller, get
from litestar.di import Provide
from litestar.response import Redirect

from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission


def provide_user_dao(session: Any) -> Any:
    """Lazy provider for AsyncUserDAO — avoids eager Flask imports."""
    from superset.db.daos.user import AsyncUserDAO

    return AsyncUserDAO(session)


class UserController(Controller):
    path = "/api/v1/user"
    tags = ["User"]
    dependencies = {
        "user_dao": Provide(provide_user_dao, sync_to_thread=False),
    }

    @get(
        "/{pk:int}/avatar.png",
        opt={"exclude_from_auth": True},
    )
    async def get_avatar(self, pk: int, user_dao: Any) -> Redirect:
        """GET /api/v1/user/{pk}/avatar.png — redirect to avatar URL."""
        user = await user_dao.get_by_id(pk)
        if user is None:
            raise ObjectNotFoundError("User", pk)

        # Check for avatar_url in extra_attributes
        avatar_url = None
        extra_attrs = getattr(user, "extra_attributes", [])
        if extra_attrs:
            first_attr = (
                extra_attrs[0]
                if isinstance(extra_attrs, list)
                else extra_attrs
            )
            avatar_url = getattr(first_attr, "avatar_url", None)

        if not avatar_url:
            # Fallback to Gravatar
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
        # Stub — pending registration model depends on FAB security backend
        return {"result": [], "count": 0}

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "UserRegistrations")],
    )
    async def get_single(self, pk: int, **kwargs: Any) -> dict[str, Any]:
        """GET /api/v1/security/user_registrations/{pk} — get single registration."""
        raise ObjectNotFoundError("UserRegistration", pk)
