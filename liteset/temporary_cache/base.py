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
"""Base temporary cache manager."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class AsyncTemporaryCacheManager:
    """Base for filter_state and form_data temporal storage."""

    def __init__(
        self,
        kv_dao: Any,
        resource: str,
    ) -> None:
        self._kv_dao = kv_dao
        self._resource = resource

    async def get(self, resource_id: int, key: str) -> str | None:
        return await self._kv_dao.get_value(
            resource=self._resource,
            resource_id=resource_id,
            key=key,
        )

    async def create(self, resource_id: int, key: str, value: str) -> None:
        await self._kv_dao.set_value(
            resource=self._resource,
            resource_id=resource_id,
            key=key,
            value=value,
        )

    async def update(self, resource_id: int, key: str, value: str) -> None:
        await self._kv_dao.set_value(
            resource=self._resource,
            resource_id=resource_id,
            key=key,
            value=value,
        )

    async def delete(self, resource_id: int, key: str) -> bool:
        return await self._kv_dao.delete_value(
            resource=self._resource,
            resource_id=resource_id,
            key=key,
        )
