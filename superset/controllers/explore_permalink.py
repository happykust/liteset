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
"""Explore permalink controller — create and resolve explore permalinks."""

from __future__ import annotations

import json
import uuid
from typing import Any

import msgspec
from litestar import Controller, get, post
from litestar.di import Provide

from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_authentication
from superset.providers import provide_kv_dao
from superset.typing import KeyValueDAOProtocol, UserProtocol


class ExplorePermalinkCreateSchema(msgspec.Struct, rename="camel"):
    """POST body for explore permalink creation.

    The original Superset endpoint accepts the *state* directly as the
    POST body: ``{formData: {...}, urlParams: [[...], ...]}``.
    The ``chartId``, ``datasourceType``, etc. are derived from
    ``formData`` inside the command, NOT sent as top-level fields.
    """

    form_data: dict[str, Any]
    url_params: list[list[str]] | None = None


class ExplorePermalinkController(Controller):
    path = "/api/v1/explore/permalink"
    tags = ["Explore Permalink"]
    dependencies = {
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
    }

    @post("/", status_code=201, guards=[require_authentication])
    async def create_permalink(
        self,
        data: ExplorePermalinkCreateSchema,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """POST /api/v1/explore/permalink/ — create permalink."""
        key = str(uuid.uuid4())[:8]
        # Derive datasource fields from formData (matches original command)
        form_data = data.form_data or {}
        datasource_str = form_data.get("datasource", "")
        parts = datasource_str.split("__") if datasource_str else []
        value = json.dumps(
            {
                "chartId": form_data.get("slice_id"),
                "datasourceId": int(parts[0])
                if len(parts) >= 1 and parts[0].isdigit()
                else None,
                "datasourceType": parts[1] if len(parts) >= 2 else "table",
                "datasource": datasource_str,
                "state": {
                    "formData": form_data,
                    "urlParams": data.url_params or [],
                },
            }
        )
        await kv_dao.set_value(
            resource="explore_permalink",
            resource_id=0,
            key=key,
            value=value,
        )
        event_logger.log("explore_permalink.create", user_id=current_user.id)
        return {"key": key, "url": f"/explore/p/{key}/"}

    @get("/{key:str}", guards=[require_authentication])
    async def get_permalink(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/explore/permalink/{key} — resolve permalink.

        The original Flask endpoint spreads the stored state fields
        directly into the response (``**value``), so the frontend
        receives ``{formData: ..., urlParams: ..., dataSources: ...}``
        rather than ``{result: {...}}``.
        """
        raw = await kv_dao.get_value(
            resource="explore_permalink",
            resource_id=0,
            key=key,
        )
        if raw is None:
            raise ObjectNotFoundError("ExplorePermalink", key)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = {"value": raw}
        # Spread fields directly into the response (matches original)
        if isinstance(data, dict):
            return data
        return {"value": data}
