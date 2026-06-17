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
"""Get command for dashboard permalinks."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandException, ObjectNotFoundError
from superset.key_value.exceptions import (
    KeyValueCodecDecodeException,
    KeyValueGetFailedError,
    KeyValueParseKeyError,
)
from superset.key_value.shared_entries import get_permalink_salt
from superset.key_value.types import KeyValueResource, SharedKey
from superset.key_value.utils import decode_permalink_id

if TYPE_CHECKING:
    from superset.db.daos.key_value import AsyncKeyValueDAO


class DashboardPermalinkGetFailedError(CommandException):
    """Raised when a dashboard permalink lookup fails."""

    message = "An error occurred while accessing the value."


class GetDashboardPermalinkCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(self, dao: AsyncKeyValueDAO, key: str) -> None:
        self._dao = dao
        self._key = key

    async def validate(self) -> None:
        pass

    async def run(self) -> dict[str, Any]:
        session: AsyncSession = self._dao.session
        salt = await get_permalink_salt(session, SharedKey.DASHBOARD_PERMALINK_SALT)
        # Parse/decode/get failures → 500 (DashboardPermalinkGetFailedError);
        # 404 is reserved for missing/expired entries only.
        try:
            entry_id = decode_permalink_id(self._key, salt=salt)
            value = await self._dao.get_value_by_key(
                resource=KeyValueResource.DASHBOARD_PERMALINK.value,
                key=entry_id,
            )
        except (
            KeyValueCodecDecodeException,
            KeyValueGetFailedError,
            KeyValueParseKeyError,
        ) as ex:
            raise DashboardPermalinkGetFailedError(
                message=getattr(ex, "message", str(ex))
            ) from ex

        if value is None:
            raise ObjectNotFoundError("DashboardPermalink", self._key)
        if isinstance(value, dict):
            return value
        return {"value": value}
