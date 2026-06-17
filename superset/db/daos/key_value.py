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
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, or_, select

from superset.db.base_dao import BaseAsyncDAO
from superset.key_value.exceptions import KeyValueCodecDecodeException
from superset.models.key_value import KeyValueEntry

Key = int | UUID


class AsyncKeyValueDAO(BaseAsyncDAO[KeyValueEntry]):
    model_cls = KeyValueEntry

    async def get_entry(
        self,
        resource: str,
        entry_id: int,
    ) -> KeyValueEntry | None:
        """No expiry filtering — callers must check ``expires_on`` themselves."""
        stmt = select(KeyValueEntry).where(
            KeyValueEntry.resource == resource,
            KeyValueEntry.id == entry_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def create_entry(
        self,
        resource: str,
        value: bytes,
        key: Key | None = None,
        expires_on: datetime | None = None,
        user_id: int | None = None,
    ) -> KeyValueEntry:
        """Create a new key-value entry.

        Key is optional and may be ``int`` (becomes entry.id) or ``UUID``
        (becomes entry.uuid). When key is None, the DB generates an
        auto-increment ``id``.

        ``user_id`` populates ``created_by_fk``. It is optional and defaults
        to ``None`` to keep the signature backward-compatible.
        """
        entry = KeyValueEntry(
            resource=resource,
            value=value,
            created_on=datetime.now(),
            created_by_fk=user_id,
            expires_on=expires_on,
        )
        if key is not None:
            if isinstance(key, UUID):
                # SA Column descriptor accepts the runtime value at assignment.
                setattr(entry, "uuid", key)  # noqa: B010
            else:
                setattr(entry, "id", key)  # noqa: B010
        self.session.add(entry)
        return entry

    async def get_entry_by_key(
        self,
        resource: str,
        key: Key,
    ) -> KeyValueEntry | None:
        """No expiry filtering — callers that need to exclude expired entries
        must check after retrieval."""
        if isinstance(key, UUID):
            filter_col = KeyValueEntry.uuid == key
        else:
            filter_col = KeyValueEntry.id == key
        stmt = select(KeyValueEntry).where(
            KeyValueEntry.resource == resource,
            filter_col,
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def get_value_by_key(
        self,
        resource: str,
        key: Key,
    ) -> Any:
        """Retrieve and JSON-decode a value; expired entries are
        treated as not found."""
        entry = await self.get_entry_by_key(resource, key)
        if entry is None:
            return None
        if entry.expires_on is not None and entry.expires_on <= datetime.now():
            return None
        import json as _json

        try:
            return _json.loads(cast("bytes", entry.value))
        except TypeError as ex:
            raise KeyValueCodecDecodeException(str(ex)) from ex

    async def upsert_entry(
        self,
        resource: str,
        entry_id: int,
        value: bytes,
        expires_on: datetime | None = None,
        user_id: int | None = None,
    ) -> KeyValueEntry:
        """Update entry if exists, otherwise create.

        Uses SELECT FOR UPDATE to prevent TOCTOU race conditions
        under concurrent access within the same transaction isolation.

        On update populates ``changed_on`` / ``changed_by_fk``; on insert
        threads ``user_id`` into ``created_by_fk``.
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
                    "changed_on": datetime.now(),
                    "changed_by_fk": user_id,
                },
            )
        return await self.create_entry(
            resource, value, expires_on=expires_on, user_id=user_id
        )

    async def delete_entry(
        self,
        resource: str,
        entry_id: int,
    ) -> bool:
        entry = await self.get_entry(resource, entry_id)
        if entry:
            await self.delete([entry])
            return True
        return False

    @staticmethod
    def _coerce_uuid(key: str) -> UUID | None:
        """Try to parse ``key`` as UUID. Return None on invalid input.

        The KeyValueEntry table stores UUIDs only, so any client-supplied
        non-UUID string cannot match an existing row. Returning None lets
        callers treat the result as a not-found instead of crashing with
        a 500 (matches original Superset, which surfaces a 404).
        """
        try:
            return UUID(key)
        except (ValueError, AttributeError, TypeError):
            return None

    async def set_value(
        self,
        resource: str,
        resource_id: int,
        key: str,
        value: str,
        user_id: int | None = None,
        expires_on: datetime | None = None,
    ) -> None:
        """Store a value keyed by resource name + UUID key.

        Raises ValueError if ``key`` is not a valid UUID.
        """
        key_uuid = self._coerce_uuid(key)
        if key_uuid is None:
            raise ValueError(f"Invalid UUID key: {key!r}")
        # No expiry filter — must find expired entries too for in-place update.
        stmt = (
            select(KeyValueEntry)
            .where(
                KeyValueEntry.resource == resource,
                KeyValueEntry.uuid == key_uuid,
            )
            .with_for_update()
        )
        result = await self.session.execute(stmt)
        existing = result.scalars().one_or_none()
        if existing:
            existing.value = value.encode("utf-8")  # type: ignore[assignment]
            existing.changed_on = datetime.now()  # type: ignore[assignment]
            existing.changed_by_fk = user_id  # type: ignore[assignment]
            if expires_on is not None:
                existing.expires_on = expires_on  # type: ignore[assignment]
        else:
            entry = KeyValueEntry(
                resource=resource,
                uuid=key_uuid,
                value=value.encode("utf-8"),
                created_on=datetime.now(),
                created_by_fk=user_id,
                expires_on=expires_on,
            )
            self.session.add(entry)
        await self.session.flush()

    async def get_value(
        self,
        resource: str,
        resource_id: int,
        key: str,
    ) -> str | None:
        """Retrieve a string value by resource name + UUID key;
        returns None for malformed UUIDs."""
        key_uuid = self._coerce_uuid(key)
        if key_uuid is None:
            return None
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
        """Delete a value by resource name + UUID key;
        returns False for malformed UUIDs."""
        key_uuid = self._coerce_uuid(key)
        if key_uuid is None:
            return False
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
        stmt = delete(KeyValueEntry).where(
            KeyValueEntry.resource == resource,
            KeyValueEntry.expires_on <= datetime.now(),
        )
        await self.session.execute(stmt)
