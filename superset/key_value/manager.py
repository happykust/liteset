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
"""Async key-value manager — codec/namespace abstraction over AsyncKeyValueDAO."""

from __future__ import annotations

import json as _json
import logging
import pickle
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from superset.db.daos.key_value import AsyncKeyValueDAO

logger = logging.getLogger(__name__)


class KeyValueCodec(ABC):
    """Abstract codec for encoding/decoding KV values."""

    @abstractmethod
    def encode(self, value: Any) -> bytes: ...

    @abstractmethod
    def decode(self, data: bytes) -> Any: ...


class PickleCodec(KeyValueCodec):
    def encode(self, value: Any) -> bytes:
        return pickle.dumps(value)

    def decode(self, data: bytes) -> Any:
        return pickle.loads(data)  # noqa: S301


class JsonCodec(KeyValueCodec):
    def encode(self, value: Any) -> bytes:
        return _json.dumps(value).encode("utf-8")

    def decode(self, data: bytes) -> Any:
        return _json.loads(data.decode("utf-8"))


class AsyncKeyValueManager:
    """High-level async key-value store with codec and namespace support.

    Wraps AsyncKeyValueDAO with encoding/decoding and namespace isolation.
    """

    def __init__(self, dao: AsyncKeyValueDAO) -> None:
        self._dao = dao

    async def get(
        self,
        resource: str,
        key: int,
        codec: KeyValueCodec | None = None,
    ) -> Any | None:
        """Get decoded value by resource + integer key (entry ID)."""
        entry = await self._dao.get_entry(resource, key)
        if entry is None:
            return None
        if codec is None:
            codec = JsonCodec()
        raw: bytes = entry.value  # type: ignore[assignment]
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return codec.decode(raw)

    async def set(
        self,
        resource: str,
        value: Any,
        key: int | None = None,
        codec: KeyValueCodec | None = None,
        expires_on: datetime | None = None,
    ) -> int:
        """Encode and upsert. Returns entry ID."""
        if codec is None:
            codec = JsonCodec()
        encoded = codec.encode(value)
        if key is not None:
            entry = await self._dao.upsert_entry(resource, key, encoded, expires_on)
        else:
            entry = await self._dao.create_entry(resource, encoded, expires_on)
        return entry.id  # type: ignore[return-value]

    async def delete(self, resource: str, key: int) -> bool:
        """Delete entry by resource + key. Returns True if deleted."""
        return await self._dao.delete_entry(resource, key)

    async def delete_expired(self, resource: str) -> None:
        """Delete all expired entries for a resource."""
        await self._dao.delete_expired_entries(resource)
