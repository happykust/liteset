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
"""Abstract base controller for temporary cache endpoints (form_data, filter_state)."""

from __future__ import annotations

import json
import uuid
from typing import Any, ClassVar

import msgspec
from litestar import Controller, delete, get, post, put

from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_authentication
from superset.typing import KeyValueDAOProtocol, UserProtocol


class TemporaryCacheSchema(msgspec.Struct):
    """Request body for temporary cache create/update."""

    value: str
    tab_id: int | None = None


class TemporaryCacheController(Controller):
    """Abstract base for ExploreFormData and other temporary cache controllers.

    Subclasses must set:
    - path: str
    - resource: str (e.g., "explore_form_data")
    """

    resource: ClassVar[str] = ""

    @get("/{key:str}", guards=[require_authentication])
    async def get_value(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /{key} — retrieve cached value."""
        # resource_id=0 for non-dashboard resources
        raw = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if raw is None:
            raise ObjectNotFoundError(self.resource, key)

        # Unwrap envelope
        try:
            entry = json.loads(raw)
            if isinstance(entry, dict) and "value" in entry:
                return {"value": entry["value"]}
        except (json.JSONDecodeError, TypeError):
            pass
        return {"value": raw}

    @post("/", status_code=201, guards=[require_authentication])
    async def create_value(
        self,
        data: TemporaryCacheSchema,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """POST / — create new cached value."""
        key = str(uuid.uuid4())
        envelope = json.dumps(
            {
                "owner": current_user.id,
                "value": data.value,
                "tab_id": data.tab_id,
            }
        )
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=key,
            value=envelope,
        )
        event_logger.log(
            f"{self.resource}.create",
            user_id=current_user.id,
        )
        return {"key": key}

    @put("/{key:str}", guards=[require_authentication])
    async def update_value(
        self,
        key: str,
        data: TemporaryCacheSchema,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """PUT /{key} — update cached value."""
        # Check exists
        existing = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if existing is None:
            raise ObjectNotFoundError(self.resource, key)

        envelope = json.dumps(
            {
                "owner": current_user.id,
                "value": data.value,
                "tab_id": data.tab_id,
            }
        )
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=key,
            value=envelope,
        )
        event_logger.log(
            f"{self.resource}.update",
            user_id=current_user.id,
        )
        return {"key": key}

    @delete("/{key:str}", status_code=200, guards=[require_authentication])
    async def delete_value(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """DELETE /{key} — delete cached value."""
        deleted = await kv_dao.delete_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if not deleted:
            raise ObjectNotFoundError(self.resource, key)
        event_logger.log(
            f"{self.resource}.delete",
            user_id=current_user.id,
        )
        return {"message": "OK"}
