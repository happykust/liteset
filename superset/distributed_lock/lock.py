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
"""Redis-based async distributed lock."""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Lua script for safe release — only deletes if value matches owner token
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class AsyncDistributedLock:
    """Redis-based async distributed lock using SET NX EX.

    Usage:
        async with AsyncDistributedLock(redis, "my-lock", timeout=30):
            # do exclusive work
    """

    def __init__(
        self,
        redis: Any,  # redis.asyncio.Redis
        key: str,
        timeout: int = 30,
    ) -> None:
        self._redis = redis
        self._key = f"superset:lock:{key}"
        self._timeout = timeout
        self._token = str(uuid.uuid4())
        self._acquired = False

    async def acquire(self) -> bool:
        """Attempt to acquire the lock. Returns True if successful."""
        if self._redis is None:
            # No Redis available — skip locking (single-instance mode)
            self._acquired = True
            return True
        result = await self._redis.set(
            self._key, self._token, nx=True, ex=self._timeout
        )
        self._acquired = result is not None and result is not False
        return self._acquired

    async def release(self) -> None:
        """Release the lock (only if we own it)."""
        if not self._acquired:
            return
        if self._redis is None:
            self._acquired = False
            return
        try:
            await self._redis.eval(_RELEASE_SCRIPT, 1, self._key, self._token)
        except Exception:
            logger.warning("Failed to release lock %s", self._key, exc_info=True)
        finally:
            self._acquired = False

    async def __aenter__(self) -> AsyncDistributedLock:
        acquired = await self.acquire()
        if not acquired:
            raise RuntimeError(f"Could not acquire lock: {self._key}")
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.release()
