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
"""Dashboard filter state controller — 4 endpoints for filter state CRUD."""

from __future__ import annotations

from litestar import Controller, delete, get, post, put
from litestar.di import Provide

from superset.commands.dashboard.filter_state.create import CreateFilterStateCommand
from superset.commands.dashboard.filter_state.delete import DeleteFilterStateCommand
from superset.commands.dashboard.filter_state.get import GetFilterStateCommand
from superset.commands.dashboard.filter_state.update import UpdateFilterStateCommand
from superset.events import event_logger
from superset.guards.rbac import require_permission
from superset.providers import provide_dashboard_dao, provide_kv_dao
from superset.schemas.dashboard import FilterStateSchema
from superset.typing import (
    DashboardDAOProtocol,
    KeyValueDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)


class DashboardFilterStateController(Controller):
    path = "/api/v1/dashboard/{pk:int}/filter_state"
    tags = ["Dashboard Filter State"]
    dependencies = {
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
        "dashboard_dao": Provide(provide_dashboard_dao, sync_to_thread=False),
    }

    @post(
        "/",
        guards=[require_permission("can_write", "DashboardFilterStateRestApi")],
        status_code=201,
    )
    async def create(
        self,
        pk: int,
        data: FilterStateSchema,
        kv_dao: KeyValueDAOProtocol,
        dashboard_dao: DashboardDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        cmd = CreateFilterStateCommand(
            dao=kv_dao,  # type: ignore[arg-type]
            dashboard_id=pk,
            value=data.value,
            user_id=current_user.id,
            tab_id=data.tab_id,
            security_manager=security_manager,
            dashboard_dao=dashboard_dao,  # type: ignore[arg-type]
            user=current_user,
        )
        key = await cmd.execute()
        await event_logger.alog_with_context(
            "filter_state.create",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"key": key}

    @put(
        "/{key:str}",
        guards=[require_permission("can_write", "DashboardFilterStateRestApi")],
    )
    async def update(
        self,
        pk: int,
        key: str,
        data: FilterStateSchema,
        kv_dao: KeyValueDAOProtocol,
        dashboard_dao: DashboardDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        cmd = UpdateFilterStateCommand(
            dao=kv_dao,  # type: ignore[arg-type]
            dashboard_id=pk,
            key=key,
            value=data.value,
            user_id=current_user.id,
            security_manager=security_manager,
            dashboard_dao=dashboard_dao,  # type: ignore[arg-type]
            user=current_user,
        )
        result_key = await cmd.execute()
        await event_logger.alog_with_context(
            "filter_state.update",
            object_ref=f"dashboard:{pk}/key:{key}",
            user_id=current_user.id,
        )
        return {"key": result_key}

    @get(
        "/{key:str}",
        guards=[require_permission("can_read", "DashboardFilterStateRestApi")],
    )
    async def get_state(
        self,
        pk: int,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        dashboard_dao: DashboardDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        cmd = GetFilterStateCommand(
            dao=kv_dao,  # type: ignore[arg-type]
            dashboard_id=pk,
            key=key,
            security_manager=security_manager,
            user_id=current_user.id,
            dashboard_dao=dashboard_dao,  # type: ignore[arg-type]
            user=current_user,
        )
        value = await cmd.execute()
        await event_logger.alog_with_context(
            "filter_state.get", object_ref=f"dashboard:{pk}/key:{key}"
        )
        return {"value": value}

    @delete(
        "/{key:str}",
        guards=[require_permission("can_write", "DashboardFilterStateRestApi")],
        status_code=200,
    )
    async def delete_state(
        self,
        pk: int,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        dashboard_dao: DashboardDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        cmd = DeleteFilterStateCommand(
            dao=kv_dao,  # type: ignore[arg-type]
            dashboard_id=pk,
            key=key,
            user_id=current_user.id,
            security_manager=security_manager,
            dashboard_dao=dashboard_dao,  # type: ignore[arg-type]
            user=current_user,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "filter_state.delete",
            object_ref=f"dashboard:{pk}/key:{key}",
            user_id=current_user.id,
        )
        return {"message": "OK"}
