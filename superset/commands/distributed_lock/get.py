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
"""Async port of ``superset_old/commands/distributed_lock/get.py``."""

from __future__ import annotations

import logging
from typing import cast

from superset.commands.distributed_lock.base import BaseDistributedLockCommand
from superset.db.daos.key_value import AsyncKeyValueDAO
from superset.distributed_lock.types import LockValue

logger = logging.getLogger(__name__)


class GetDistributedLock(BaseDistributedLockCommand):
    """Return the decoded LockValue for ``self.key`` or ``None``.

    Mirrors the sync original; the underlying ``get_entry_by_key`` already
    filters out expired rows so no second ``is_expired()`` check is needed.
    """

    async def run(self) -> LockValue | None:
        dao = AsyncKeyValueDAO(self.session)
        entry = await dao.get_entry_by_key(self.resource, self.key)
        if entry is None:
            return None
        raw = cast("bytes | str", entry.value)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return cast(LockValue, self.codec.decode(raw))
