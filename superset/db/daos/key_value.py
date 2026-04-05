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
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, or_, select

from superset.db.base_dao import BaseAsyncDAO
from superset.models.key_value import KeyValueEntry


class AsyncKeyValueDAO(BaseAsyncDAO[KeyValueEntry]):
    model_cls = KeyValueEntry

    async def get_entry(
        self,
        resource: str,
        entry_id: int,
    ) -> KeyValueEntry | None:
        """Get a non-expired key-value entry by resource and integer ID."""
        stmt = select(KeyValueEntry).where(
            KeyValueEntry.resource == resource,
            KeyValueEntry.id == entry_id,
            or_(
                KeyValueEntry.expires_on.is_(None),
                KeyValueEntry.expires_on > datetime.now(),
            ),
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def create_entry(
        self,
        resource: str,
        value: bytes,
        expires_on: datetime | None = None,
    ) -> KeyValueEntry:
        """Create a new key-value entry."""
        return await self.create(
            {
                "resource": resource,
                "value": value,
                "expires_on": expires_on,
            }
        )

    async def upsert_entry(
        self,
        resource: str,
        entry_id: int,
        value: bytes,
        expires_on: datetime | None = None,
    ) -> KeyValueEntry:
        """Update entry if exists, otherwise create.

        Uses SELECT FOR UPDATE to prevent TOCTOU race conditions
        under concurrent access within the same transaction isolation.
        """
        stmt = (
            select(KeyValueEntry)
            .where(
                KeyValueEntry.resource == resource,
                KeyValueEntry.id == entry_id,
            )
            .with_for_update()
        )
        result = await self.session.execute(stmt)
        existing = result.scalars().one_or_none()
        if existing:
            return await self.update(
                existing,
                {
                    "value": value,
                    "expires_on": expires_on,
                },
            )
        return await self.create_entry(resource, value, expires_on)

    async def delete_entry(
        self,
        resource: str,
        entry_id: int,
    ) -> bool:
        """Delete a key-value entry. Returns True if deleted."""
        entry = await self.get_entry(resource, entry_id)
        if entry:
            await self.delete([entry])
            return True
        return False

    # ------------------------------------------------------------------
    # High-level string-keyed API for filter state / permalinks
    #
    # The original Superset stores:
    #   - ``resource`` column (VARCHAR 32): short resource name, e.g.
    #     "explore_form_data"
    #   - ``uuid`` column: the lookup key (a UUID)
    #   - ``value`` column: the payload (bytes)
    # ------------------------------------------------------------------

    async def set_value(
        self,
        resource: str,
        resource_id: int,
        key: str,
        value: str,
    ) -> None:
        """Store a string value keyed by resource name + UUID key."""
        import uuid as _uuid

        key_uuid = _uuid.UUID(key)
        stmt = (
            select(KeyValueEntry)
            .where(
                KeyValueEntry.resource == resource,
                KeyValueEntry.uuid == key_uuid,
                or_(
                    KeyValueEntry.expires_on.is_(None),
                    KeyValueEntry.expires_on > datetime.now(),
                ),
            )
            .with_for_update()
        )
        result = await self.session.execute(stmt)
        existing = result.scalars().one_or_none()
        if existing:
            existing.value = value.encode("utf-8")  # type: ignore[assignment]
        else:
            entry = KeyValueEntry(
                resource=resource,
                uuid=key_uuid,
                value=value.encode("utf-8"),
            )
            self.session.add(entry)
        await self.session.flush()

    async def get_value(
        self,
        resource: str,
        resource_id: int,
        key: str,
    ) -> str | None:
        """Retrieve a string value by resource name + UUID key."""
        import uuid as _uuid

        key_uuid = _uuid.UUID(key)
        stmt = select(KeyValueEntry).where(
            KeyValueEntry.resource == resource,
            KeyValueEntry.uuid == key_uuid,
            or_(
                KeyValueEntry.expires_on.is_(None),
                KeyValueEntry.expires_on > datetime.now(),
            ),
        )
        result = await self.session.execute(stmt)
        entry = result.scalars().one_or_none()
        if entry is None:
            return None
        return entry.value.decode("utf-8")

    async def delete_value(
        self,
        resource: str,
        resource_id: int,
        key: str,
    ) -> bool:
        """Delete a value by resource name + UUID key."""
        import uuid as _uuid

        key_uuid = _uuid.UUID(key)
        stmt = select(KeyValueEntry).where(
            KeyValueEntry.resource == resource,
            KeyValueEntry.uuid == key_uuid,
        )
        result = await self.session.execute(stmt)
        entry = result.scalars().one_or_none()
        if entry:
            await self.session.delete(entry)
            await self.session.flush()
            return True
        return False

    async def delete_expired_entries(self, resource: str) -> None:
        """Delete all expired entries for a resource."""
        stmt = delete(KeyValueEntry).where(
            KeyValueEntry.resource == resource,
            KeyValueEntry.expires_on <= datetime.now(),
        )
        await self.session.execute(stmt)
