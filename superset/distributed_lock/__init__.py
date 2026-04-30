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
"""Async port of ``superset_old/distributed_lock/__init__.py``.

Provides :func:`KeyValueDistributedLock`, an ``@asynccontextmanager``
that acquires a database-backed (key-value) distributed lock.  The
sync original used Flask's global ``db.session``; the async port takes
an explicit :class:`~sqlalchemy.ext.asyncio.AsyncSession` because there
is no async-session global in Litestar.

The kwargs accepted by the context manager (``user_id``, ``database_id``,
…) are passed verbatim to :func:`superset.distributed_lock.utils.get_key`
to derive a deterministic UUID5 key — identical behaviour to the original.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, TYPE_CHECKING

from superset.distributed_lock.utils import get_key
from superset.exceptions import CreateKeyValueDistributedLockFailedException
from superset.key_value.manager import JsonCodec
from superset.key_value.types import KeyValueResource

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

CODEC = JsonCodec()
LOCK_EXPIRATION = timedelta(seconds=30)
RESOURCE = KeyValueResource.LOCK


@asynccontextmanager
async def KeyValueDistributedLock(  # pylint: disable=invalid-name  # noqa: N802
    namespace: str,
    session: "AsyncSession",
    **kwargs: Any,
) -> AsyncIterator[uuid.UUID]:
    """KV global lock for refreshing tokens (and other namespaced critical sections).

    This context manager acquires a distributed lock for a given namespace,
    with optional parameters (e.g. ``namespace="refresh_oauth2_token",
    user_id=1, database_id=2``).  It yields a UUID for the lock that can
    be used within the context, and corresponds to the key in the KV store.

    :param namespace: The namespace for which the lock is to be acquired.
    :param session: The async session used to talk to the metadata DB.
    :param kwargs: Additional parameters that contribute to the lock key.
    :yields: A unique identifier (UUID) for the acquired lock (the KV key).
    :raises CreateKeyValueDistributedLockFailedException: If the lock is taken.
    """

    # pylint: disable=import-outside-toplevel
    from superset.commands.distributed_lock.create import CreateDistributedLock
    from superset.commands.distributed_lock.delete import DeleteDistributedLock
    from superset.commands.distributed_lock.get import GetDistributedLock

    key = get_key(namespace, **kwargs)
    value = await GetDistributedLock(
        session=session, namespace=namespace, params=kwargs
    ).run()
    if value:
        logger.debug("Lock on namespace %s for key %s already taken", namespace, key)
        raise CreateKeyValueDistributedLockFailedException("Lock already taken")

    logger.debug("Acquiring lock on namespace %s for key %s", namespace, key)
    try:
        await CreateDistributedLock(
            session=session, namespace=namespace, params=kwargs
        ).run()
    except CreateKeyValueDistributedLockFailedException as ex:
        logger.debug("Lock on namespace %s for key %s already taken", namespace, key)
        raise CreateKeyValueDistributedLockFailedException("Lock already taken") from ex

    try:
        yield key
    finally:
        await DeleteDistributedLock(
            session=session, namespace=namespace, params=kwargs
        ).run()
        logger.debug("Removed lock on namespace %s for key %s", namespace, key)
