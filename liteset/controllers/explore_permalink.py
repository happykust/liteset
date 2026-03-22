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

from liteset.events import event_logger
from liteset.exceptions import ObjectNotFoundError
from liteset.providers import provide_kv_dao
from liteset.typing import KeyValueDAOProtocol, UserProtocol


class ExplorePermalinkCreateBody(msgspec.Struct):
    """POST body for explore permalink creation."""

    chart_id: int | None = None
    form_data: dict[str, Any] = {}
    url_params: dict[str, str] = {}


class ExplorePermalinkController(Controller):
    path = "/api/v1/explore/permalink"
    tags = ["Explore Permalink"]
    dependencies = {
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
    }

    @post("/", status_code=201)
    async def create_permalink(
        self,
        data: ExplorePermalinkCreateBody,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """POST /api/v1/explore/permalink/ — create permalink."""
        key = str(uuid.uuid4())[:8]
        value = json.dumps(msgspec.to_builtins(data))
        await kv_dao.set_value(
            resource="explore_permalink",
            resource_id=0,
            key=key,
            value=value,
        )
        event_logger.log("explore_permalink.create", user_id=current_user.id)
        return {"key": key, "url": f"/explore/p/{key}/"}

    @get("/{key:str}")
    async def get_permalink(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/explore/permalink/{key} — resolve permalink."""
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
        return {"result": data}
