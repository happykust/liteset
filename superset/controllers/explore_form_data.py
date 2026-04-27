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
"""Explore form data controller — temporary cache for explore form state.

The frontend sends ``{datasource_id, datasource_type, form_data, chart_id?}``
with ``tab_id`` as a query parameter.  The original Superset stores the
serialized value in a KV table keyed by a UUID.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.di import Provide
from litestar.params import Parameter

from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import (
    deny_anon_with_403,
    require_authentication,
    require_permission,
)
from superset.providers import provide_kv_dao
from superset.typing import KeyValueDAOProtocol, UserProtocol

DatasourceType = Literal["table", "dataset", "query", "saved_query", "view"]


class FormDataPostSchema(msgspec.Struct):
    """POST body matching the original Superset explore form_data API."""

    datasource_id: int
    datasource_type: DatasourceType
    form_data: str
    chart_id: int | None = None


class FormDataPutSchema(msgspec.Struct):
    """PUT body matching the original Superset explore form_data API."""

    datasource_id: int
    datasource_type: DatasourceType
    form_data: str
    chart_id: int | None = None


class ExploreFormDataController(Controller):
    path = "/api/v1/explore/form_data"
    tags = ["Explore Form Data"]
    resource = "explore_form_data"
    dependencies = {
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
    }

    @get("/{key:str}", guards=[require_authentication])
    async def get_value(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /{key} — retrieve cached form_data."""
        raw = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if raw is None:
            raise ObjectNotFoundError(self.resource, key)

        try:
            entry = json.loads(raw)
            if isinstance(entry, dict) and "value" in entry:
                return {"form_data": entry["value"]}
        except (json.JSONDecodeError, TypeError):
            pass
        return {"form_data": raw}

    @post(
        "/",
        status_code=201,
        guards=[
            deny_anon_with_403,
            require_permission("can_write", "ExploreFormDataRestApi"),
        ],
    )
    async def create_value(
        self,
        data: FormDataPostSchema,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
        tab_id: int | None = Parameter(query="tab_id", default=None, required=False),
    ) -> dict[str, str]:
        """POST / — create new cached form_data."""
        key = str(uuid.uuid4())
        envelope = json.dumps(
            {
                "owner": current_user.id,
                "datasource_id": data.datasource_id,
                "datasource_type": data.datasource_type,
                "chart_id": data.chart_id,
                "tab_id": tab_id,
                "value": data.form_data,
            }
        )
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=key,
            value=envelope,
        )
        await event_logger.alog_with_context(
            "explore_form_data.create",
            user_id=current_user.id,
        )
        return {"key": key}

    @put("/{key:str}", guards=[require_authentication])
    async def update_value(
        self,
        key: str,
        data: FormDataPutSchema,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
        tab_id: int | None = Parameter(query="tab_id", default=None, required=False),
    ) -> dict[str, str]:
        """PUT /{key} — update cached form_data."""
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
                "datasource_id": data.datasource_id,
                "datasource_type": data.datasource_type,
                "chart_id": data.chart_id,
                "tab_id": tab_id,
                "value": data.form_data,
            }
        )
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=key,
            value=envelope,
        )
        await event_logger.alog_with_context(
            "explore_form_data.update",
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
        """DELETE /{key} — delete cached form_data."""
        deleted = await kv_dao.delete_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if not deleted:
            raise ObjectNotFoundError(self.resource, key)
        await event_logger.alog_with_context(
            "explore_form_data.delete",
            user_id=current_user.id,
        )
        return {"message": "OK"}
