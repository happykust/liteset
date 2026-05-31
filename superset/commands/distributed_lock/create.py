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
"""Async port of ``superset_old/commands/distributed_lock/create.py``."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError

from superset.commands.distributed_lock.base import BaseDistributedLockCommand
from superset.db.daos.key_value import AsyncKeyValueDAO
from superset.exceptions import CreateKeyValueDistributedLockFailedException
from superset.key_value.types import KeyValueResource

logger = logging.getLogger(__name__)


class CreateDistributedLock(BaseDistributedLockCommand):
    """Insert a fresh ``LOCK`` row for ``self.key``.

    Mirrors the sync original: deletes expired entries first, then inserts a
    new one.  Any encoding / SQL error is rewrapped as
    :class:`CreateKeyValueDistributedLockFailedException`, matching the
    ``@transaction(on_error=...reraise=...)`` decorator on the original.
    Transaction commit is handled by ``provide_async_session`` at the
    request boundary; this method only flushes.

    The INSERT is wrapped in a SAVEPOINT (``begin_nested``): when two
    workers race for the same lock the loser hits a UNIQUE
    ``IntegrityError``.  Without the savepoint that failed INSERT poisons
    the shared request session (every later statement fails), which is
    exactly the path the OAuth2 ``@backoff`` retry walks into.  Rolling
    back only the savepoint keeps the outer request transaction intact —
    the same pattern as ``tags/core.py::get_tag``.  This stands in for the
    original's ``@transaction`` rollback.
    """

    lock_expiration = timedelta(seconds=30)

    async def run(self) -> None:
        dao = AsyncKeyValueDAO(self.session)
        try:
            await dao.delete_expired_entries(self.resource)
            try:
                value_bytes = self.codec.encode({"value": True})
            except Exception as ex:  # noqa: BLE001 — match KeyValueCodecEncodeException
                raise CreateKeyValueDistributedLockFailedException(
                    "Lock encoding failed"
                ) from ex
            async with self.session.begin_nested():
                await dao.create_entry(
                    resource=KeyValueResource.LOCK,
                    value=value_bytes,
                    key=self.key,
                    expires_on=datetime.now() + self.lock_expiration,
                )
                await self.session.flush()
        except CreateKeyValueDistributedLockFailedException:
            raise
        except SQLAlchemyError as ex:
            raise CreateKeyValueDistributedLockFailedException(
                "Lock acquisition failed"
            ) from ex
