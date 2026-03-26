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
"""Dashboard filter state command classes."""

from __future__ import annotations

import json  # noqa: TID251
import uuid
from typing import Any, TYPE_CHECKING

from liteset.commands.base import AsyncBaseCommand
from liteset.exceptions import ForbiddenError, ObjectNotFoundError

if TYPE_CHECKING:
    from liteset.db.daos.key_value import AsyncKeyValueDAO


async def _check_dashboard_access(
    dao: AsyncKeyValueDAO,
    dashboard_id: int,
    user_id: int,
    security_manager: Any,
) -> None:
    """Verify user can access the given dashboard.

    Uses security_manager.raise_for_dashboard_access(dashboard_id, user_id)
    if available; otherwise falls back to loading the Dashboard ORM model
    and calling can_access_dashboard().
    """
    # Load the ORM model via lazy import (may fail in minimal envs)
    try:
        from liteset.models.dashboard import Dashboard  # noqa: TID253
    except (ImportError, ModuleNotFoundError):
        # If superset models are unavailable (e.g. test env without full
        # superset stack), fall back to a basic permission check.
        user = await security_manager.find_user_by_id(user_id)
        if user is None:
            raise ForbiddenError("User not found")
        has = await security_manager.has_access(
            "can_read", "Dashboard", user=user
        )
        if not has:
            raise ForbiddenError("User does not have access to this dashboard")
        return

    dashboard = await dao.session.get(Dashboard, dashboard_id)
    if dashboard is None:
        raise ObjectNotFoundError("Dashboard", dashboard_id)
    user = await security_manager.find_user_by_id(user_id)
    if user is None:
        raise ForbiddenError("User not found")
    if hasattr(security_manager, "can_access_dashboard"):
        has_access = await security_manager.can_access_dashboard(dashboard, user=user)
        if not has_access:
            raise ForbiddenError("User does not have access to this dashboard")


class CreateFilterStateCommand(AsyncBaseCommand[str]):
    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        dashboard_id: int,
        value: str,
        user_id: int,
        tab_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._value = value
        self._user_id = user_id
        self._tab_id = tab_id
        self._security_manager = security_manager

    async def validate(self) -> None:
        # Check that user has access to the dashboard
        if self._security_manager is not None:
            await _check_dashboard_access(
                self._dao, self._dashboard_id, self._user_id, self._security_manager
            )

    async def run(self) -> str:
        # TODO(CMD-I11): Add contextual key generation using session ID and
        # resource ID to prevent duplicate filter states. Requires access to
        # the session/request context which is not available in the command layer.
        key = str(uuid.uuid4())
        envelope = json.dumps({"owner": self._user_id, "value": self._value})
        await self._dao.set_value(
            resource="dashboard_filter_state",
            resource_id=self._dashboard_id,
            key=key,
            value=envelope,
        )
        return key


class UpdateFilterStateCommand(AsyncBaseCommand[str]):
    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        dashboard_id: int,
        key: str,
        value: str,
        user_id: int,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._key = key
        self._value = value
        self._user_id = user_id
        self._security_manager = security_manager

    async def validate(self) -> None:
        # Check that user has access to the dashboard
        if self._security_manager is not None:
            await _check_dashboard_access(
                self._dao, self._dashboard_id, self._user_id, self._security_manager
            )
        existing = await self._dao.get_value(
            resource="dashboard_filter_state",
            resource_id=self._dashboard_id,
            key=self._key,
        )
        if existing is None:
            raise ObjectNotFoundError("FilterState", self._key)
        # Check ownership
        try:
            entry = json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            entry = {}
        owner = entry.get("owner")
        if owner is not None and owner != self._user_id:
            raise ForbiddenError("Only the owner can update this filter state")
        if owner is None and existing:
            raise ForbiddenError("Cannot update filter state with unknown ownership")

    async def run(self) -> str:
        envelope = json.dumps({"owner": self._user_id, "value": self._value})
        await self._dao.set_value(
            resource="dashboard_filter_state",
            resource_id=self._dashboard_id,
            key=self._key,
            value=envelope,
        )
        return self._key


class GetFilterStateCommand(AsyncBaseCommand[str]):
    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        dashboard_id: int,
        key: str,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._key = key
        self._security_manager = security_manager
        self._user_id = user_id

    async def validate(self) -> None:
        # Check that user has access to the dashboard
        if self._security_manager is not None and self._user_id is not None:
            await _check_dashboard_access(
                self._dao, self._dashboard_id, self._user_id, self._security_manager
            )

    async def run(self) -> str:
        raw = await self._dao.get_value(
            resource="dashboard_filter_state",
            resource_id=self._dashboard_id,
            key=self._key,
        )
        if raw is None:
            raise ObjectNotFoundError("FilterState", self._key)
        # Unwrap envelope written by Create/Update commands
        try:
            entry = json.loads(raw)
            if isinstance(entry, dict) and "value" in entry:
                return entry["value"]
        except (json.JSONDecodeError, TypeError):
            pass
        return raw


class DeleteFilterStateCommand(AsyncBaseCommand[bool]):
    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        dashboard_id: int,
        key: str,
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._key = key
        self._user_id = user_id
        self._security_manager = security_manager

    async def validate(self) -> None:
        # Check that user has access to the dashboard
        if self._security_manager is not None and self._user_id is not None:
            await _check_dashboard_access(
                self._dao, self._dashboard_id, self._user_id, self._security_manager
            )

    async def run(self) -> bool:
        return await self._dao.delete_value(
            resource="dashboard_filter_state",
            resource_id=self._dashboard_id,
            key=self._key,
        )
