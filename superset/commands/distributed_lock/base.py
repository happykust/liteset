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
"""Base async command for KV-backed distributed-lock operations.

Uses an explicit :class:`~sqlalchemy.ext.asyncio.AsyncSession` injected via
``__init__`` rather than a global database session.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from superset.commands.base import AsyncBaseCommand
from superset.distributed_lock.utils import get_key
from superset.key_value.manager import JsonCodec
from superset.key_value.types import KeyValueResource

logger = logging.getLogger(__name__)


class BaseDistributedLockCommand(AsyncBaseCommand[Any]):
    """Shared state for the create/delete/get lock commands.

    Computes the lock key from ``(namespace, params)`` in ``__init__``.
    ``codec`` and ``resource`` are class-level attributes shared by all
    subcommands.
    """

    key: uuid.UUID
    codec = JsonCodec()
    resource = KeyValueResource.LOCK

    def __init__(
        self,
        session: AsyncSession,
        namespace: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.session = session
        self.key = get_key(namespace, **(params or {}))

    async def validate(self) -> None:
        return None
