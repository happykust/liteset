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
"""SqlLab permalink controller — 2 endpoints for create and resolve."""

from __future__ import annotations

from typing import Any

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.di import Provide
from litestar.exceptions import ValidationException

from superset.commands.sqllab import (
    CreateSqlLabPermalinkCommand,
    GetSqlLabPermalinkCommand,
)
from superset.events import event_logger
from superset.guards.rbac import require_permission
from superset.providers import provide_kv_dao
from superset.schemas.sqllab import SqlLabPermalinkSchema
from superset.typing import KeyValueDAOProtocol, UserProtocol


class SqlLabPermalinkController(Controller):
    path = "/api/v1/sqllab/permalink"
    tags = ["SqlLab Permalink"]
    dependencies = {"kv_dao": Provide(provide_kv_dao, sync_to_thread=False)}

    @post(
        "/",
        guards=[require_permission("can_write", "SqlLabPermalinkRestApi")],
        status_code=201,
    )
    async def create_permalink(
        self,
        request: Request[Any, Any, Any],
        data: SqlLabPermalinkSchema,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        # Reject explicit null for ``autorun`` — original Marshmallow
        # ``fields.Boolean()`` (no allow_none=True) rejects null with 400.
        # msgspec can't distinguish absent vs null for an optional field, so we
        # check the raw body here, mirroring Marshmallow's contract exactly.
        raw = await request.json()
        if isinstance(raw, dict) and "autorun" in raw and raw["autorun"] is None:
            raise ValidationException(
                detail="Validation error",
                extra={"messages": {"autorun": ["Field may not be null."]}},
            )
        # Build the state dict from the typed fields (1:1 with the original
        # Marshmallow schema fields stored as state). Required: dbId, name, sql.
        state: dict[str, Any] = {
            "dbId": data.db_id,
            "name": data.name,
            "sql": data.sql,
        }
        # Optional fields — only include when present or explicitly null.
        # Original Marshmallow fields.String(allow_none=True) preserves
        # explicit JSON null as None in the loaded dict; mirror that here
        # by checking the raw body for explicit nulls vs absent keys.
        # This ensures the deterministic UUID and round-trip GET match the
        # original for clients that send schema/catalog/templateParams as null.
        for field_name, state_key in [
            ("schema", "schema"),
            ("catalog", "catalog"),
            ("templateParams", "templateParams"),
        ]:
            if isinstance(raw, dict) and field_name in raw:
                # Key was present in request (either null or non-null)
                if raw[field_name] is None:
                    state[state_key] = None
                # Non-null case handled below via data attributes
        if data.schema is not None:
            state["schema"] = data.schema
        if data.catalog is not None:
            state["catalog"] = data.catalog
        if data.autorun is not None:
            state["autorun"] = data.autorun
        if data.template_params is not None:
            state["templateParams"] = data.template_params
        cmd = CreateSqlLabPermalinkCommand(
            dao=kv_dao,  # type: ignore[arg-type]
            state=state,
            user_id=current_user.id,
        )
        key = await cmd.execute()
        await event_logger.alog_with_context("sqllab_permalink.create")
        # 1:1 with upstream
        # ``url_for("SqllabView.permalink_view", ..., _external=True)``
        # whose ``route_base="/sqllab"`` yields ``/sqllab/p/{key}/``.
        # _external=True produces a fully-qualified absolute URL; replicate that
        # here by prepending the scheme+host from the incoming request.
        base = str(request.base_url).rstrip("/")
        return {"key": key, "url": f"{base}/sqllab/p/{key}/"}

    @get(
        "/{key:str}",
        guards=[require_permission("can_read", "SqlLabPermalinkRestApi")],
    )
    async def get_permalink(
        self, key: str, kv_dao: KeyValueDAOProtocol
    ) -> dict[str, Any]:
        cmd = GetSqlLabPermalinkCommand(dao=kv_dao, key=key)  # type: ignore[arg-type]
        state = await cmd.execute()
        await event_logger.alog_with_context(
            "sqllab_permalink.get", object_ref=f"permalink:{key}"
        )
        return state
