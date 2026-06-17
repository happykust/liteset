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
"""Shared key-value entries — permalink salts, etc.

Stores well-known singleton values (e.g. permalink hashing salts) in the
``key_value`` table under the ``app`` resource, keyed by a
deterministic UUID derived from the SharedKey name.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid3

from sqlalchemy.ext.asyncio import AsyncSession

from superset.db.daos.key_value import AsyncKeyValueDAO
from superset.key_value.types import KeyValueResource, SharedKey
from superset.key_value.utils import get_uuid_namespace, random_key

RESOURCE = KeyValueResource.APP
NAMESPACE = get_uuid_namespace("")


async def get_shared_value(session: AsyncSession, key: SharedKey) -> Any | None:
    """Return a previously-stored shared value, or None."""
    dao = AsyncKeyValueDAO(session)
    uuid_key = uuid3(NAMESPACE, key.value)
    return await dao.get_value_by_key(RESOURCE.value, uuid_key)


async def set_shared_value(
    session: AsyncSession,
    key: SharedKey,
    value: Any,
) -> None:
    """Create a shared value entry keyed by a deterministic UUID."""
    dao = AsyncKeyValueDAO(session)
    uuid_key = uuid3(NAMESPACE, key.value)
    encoded = json.dumps(value).encode("utf-8")
    await dao.create_entry(RESOURCE.value, encoded, key=uuid_key)
    await session.flush()


async def get_permalink_salt(session: AsyncSession, key: SharedKey) -> str:
    """Get or create a permalink hashing salt.

    If the salt doesn't exist yet, generates a new 48-byte random
    salt and persists it.
    """
    salt = await get_shared_value(session, key)
    if salt is None:
        salt = random_key(48)
        await set_shared_value(session, key, salt)
    return salt
