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
"""Async distributed lock backed by the KV store.

Provides :func:`KeyValueDistributedLock`, an ``@asynccontextmanager``
that acquires a database-backed (key-value) distributed lock.  The
sync original used the global ``db.session``; the async port takes
an explicit :class:`~sqlalchemy.ext.asyncio.AsyncSession` because there
is no async-session global in Litestar.

The kwargs accepted by the context manager (``user_id``, ``database_id``,
…) are passed verbatim to :func:`superset.distributed_lock.utils.get_key`
to derive a deterministic UUID5 key — identical behaviour to the original.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from sqlalchemy import delete, or_

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

    yield key
    await DeleteDistributedLock(
        session=session, namespace=namespace, params=kwargs
    ).run()
    logger.debug("Removed lock on namespace %s for key %s", namespace, key)


@contextmanager
def sync_key_value_distributed_lock(  # noqa: N802
    namespace: str,
    **kwargs: Any,
) -> Iterator[uuid.UUID]:
    """Synchronous KV-backed distributed lock (sibling of
    :func:`KeyValueDistributedLock`).

    Uses the three ``CreateDistributedLock`` / ``GetDistributedLock`` /
    ``DeleteDistributedLock`` commands driven from the sync metadata
    session (``get_sync_session`` / psycopg2) rather than the async session.
    The KV operations are inlined against
    :class:`~superset.models.key_value.KeyValueEntry` via
    :class:`~superset.db.daos.key_value.AsyncKeyValueDAO`
    (``delete_expired_entries`` -> ``get_entry_by_key`` -> ``create_entry``
    on acquire; ``get_entry_by_key`` -> delete on release). Reuses the same
    :func:`get_key` UUID5 derivation, :class:`JsonCodec`,
    ``KeyValueResource.LOCK`` resource, and ``LOCK_EXPIRATION`` as the async
    path.

    Used by :func:`superset.utils.oauth2.sync_refresh_oauth2_token` so
    concurrent sync refreshes for the same ``(user_id, database_id)`` pair
    serialise on the IDP exchange.

    :param namespace: Namespace for the lock (e.g. ``"refresh_oauth2_token"``).
    :param kwargs: Parameters contributing to the deterministic lock key.
    :yields: The UUID key of the acquired lock.
    :raises CreateKeyValueDistributedLockFailedException: If the lock is taken.
    """
    from superset.db.session import get_sync_session
    from superset.models.key_value import KeyValueEntry

    key = get_key(namespace, **kwargs)
    resource = RESOURCE.value

    with get_sync_session() as session:
        existing = (
            session.query(KeyValueEntry)
            .filter(
                KeyValueEntry.resource == resource,
                KeyValueEntry.uuid == key,
                or_(
                    KeyValueEntry.expires_on.is_(None),
                    KeyValueEntry.expires_on > datetime.now(),
                ),
            )
            .one_or_none()
        )
        if existing is not None:
            logger.debug(
                "Lock on namespace %s for key %s already taken", namespace, key
            )
            raise CreateKeyValueDistributedLockFailedException("Lock already taken")

        logger.debug("Acquiring lock on namespace %s for key %s", namespace, key)
        session.execute(
            delete(KeyValueEntry).where(
                KeyValueEntry.resource == resource,
                KeyValueEntry.expires_on <= datetime.now(),
            )
        )
        entry = KeyValueEntry(
            resource=resource,
            value=CODEC.encode({"value": True}),
            created_on=datetime.now(),
            expires_on=datetime.now() + LOCK_EXPIRATION,
        )
        entry.uuid = key
        session.add(entry)
        session.flush()
        session.commit()

        yield key
        row = (
            session.query(KeyValueEntry)
            .filter(
                KeyValueEntry.resource == resource,
                KeyValueEntry.uuid == key,
            )
            .one_or_none()
        )
        if row is not None:
            session.delete(row)
            session.commit()
        logger.debug("Removed lock on namespace %s for key %s", namespace, key)
