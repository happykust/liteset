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
from litestar.di import Provide

from superset.commands.sqllab import (
    CreateSqlLabPermalinkCommand,
    GetSqlLabPermalinkCommand,
)
from superset.events import event_logger
from superset.guards.rbac import require_permission
from superset.providers import provide_kv_dao
from superset.schemas.sqllab import SqlLabPermalinkSchema
from superset.typing import KeyValueDAOProtocol


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
        self, data: SqlLabPermalinkSchema, kv_dao: KeyValueDAOProtocol
    ) -> dict[str, str]:
        cmd = CreateSqlLabPermalinkCommand(dao=kv_dao, state=data.state)
        key = await cmd.execute()
        event_logger.log("sqllab_permalink.create")
        return {"key": key, "url": f"/api/v1/sqllab/permalink/{key}"}

    @get(
        "/{key:str}",
        guards=[require_permission("can_read", "SqlLabPermalinkRestApi")],
    )
    async def get_permalink(
        self, key: str, kv_dao: KeyValueDAOProtocol
    ) -> dict[str, Any]:
        cmd = GetSqlLabPermalinkCommand(dao=kv_dao, key=key)
        state = await cmd.execute()
        event_logger.log("sqllab_permalink.get", object_ref=f"permalink:{key}")
        return state
