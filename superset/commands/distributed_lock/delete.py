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
"""Command to delete a distributed lock entry from the key-value store."""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError

from superset.commands.distributed_lock.base import BaseDistributedLockCommand
from superset.db.daos.key_value import AsyncKeyValueDAO
from superset.exceptions import DeleteKeyValueDistributedLockFailedException

logger = logging.getLogger(__name__)


class DeleteDistributedLock(BaseDistributedLockCommand):
    """Delete the ``LOCK`` row for ``self.key`` if present.

    SQL errors are re-raised as
    :class:`DeleteKeyValueDistributedLockFailedException`.
    """

    async def run(self) -> None:
        dao = AsyncKeyValueDAO(self.session)
        try:
            entry = await dao.get_entry_by_key(self.resource, self.key)
            if entry is not None:
                await self.session.delete(entry)
                await self.session.flush()
        except SQLAlchemyError as ex:
            raise DeleteKeyValueDistributedLockFailedException(
                "Lock release failed"
            ) from ex
