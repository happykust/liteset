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

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import CursorResult, delete as sa_delete, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

T = TypeVar("T", bound=DeclarativeBase)


class BaseAsyncDAO(Generic[T]):
    model_cls: type[T]
    # Shared across all subclasses intentionally: keyed by model_cls,
    # so each subclass caches its own PK column without collision.
    _pk_column_cache: dict[type, Any] = {}  # noqa: RUF012

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @classmethod
    def _get_pk_column(cls) -> Any:
        """Return the primary key column attribute, cached per model class."""
        try:
            return cls._pk_column_cache[cls.model_cls]
        except KeyError:
            pk_cols = inspect(cls.model_cls).primary_key
            if len(pk_cols) != 1:
                raise ValueError(
                    f"{cls.model_cls.__name__} has composite PK; "
                    "use a custom query instead of find_by_ids"
                )
            col = getattr(cls.model_cls, pk_cols[0].name)
            cls._pk_column_cache[cls.model_cls] = col
            return col

    async def find_by_id(self, model_id: int | str) -> T | None:
        return await self.session.get(self.model_cls, model_id)

    async def find_by_ids(self, model_ids: Sequence[int | str]) -> list[T]:
        pk_col = self._get_pk_column()
        stmt = select(self.model_cls).where(pk_col.in_(model_ids))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_all(
        self,
        filters: list[Any] | None = None,
        page: int = 0,
        page_size: int = 0,
        order_by: list[Any] | None = None,
        options: list[Any] | None = None,
    ) -> list[T]:
        stmt = select(self.model_cls)
        if options:
            stmt = stmt.options(*options)
        if filters:
            stmt = stmt.where(*filters)
        if page_size > 0:
            tiebreak = self._get_pk_column()
            if order_by:
                stmt = stmt.order_by(*order_by, tiebreak)
            else:
                stmt = stmt.order_by(tiebreak)
            stmt = stmt.offset(page * page_size).limit(page_size)
        elif order_by:
            stmt = stmt.order_by(*order_by)
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def count(self, filters: list[Any] | None = None) -> int:
        """Return total record count (for pagination metadata)."""
        stmt = select(func.count()).select_from(self.model_cls)
        if filters:
            stmt = stmt.where(*filters)
        result = await self.session.scalar(stmt)
        return result or 0

    async def find_one_or_none(self, **filter_by: Any) -> T | None:
        stmt = select(self.model_cls).filter_by(**filter_by)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> T:
        """Create a new model instance and add to session.

        Note: Does not flush or commit. Call session.flush() or session.commit()
        at the command/controller level.
        """
        item = self.model_cls(**attributes)
        self.session.add(item)
        return item

    async def update(self, item: T, attributes: dict[str, Any]) -> T:
        """Update model attributes in-place.

        Note: Does not flush or commit. Changes are tracked by the session
        and persisted on next flush/commit.
        """
        for key, value in attributes.items():
            setattr(item, key, value)
        return item

    async def delete(self, items: list[T]) -> None:
        for item in items:
            await self.session.delete(item)

    async def bulk_delete(self, ids: list[int | str]) -> int:
        """Bulk delete by IDs in a single SQL DELETE. Returns deleted count.

        WARNING: Bypasses ORM-level cascades (cascade="all, delete-orphan").
        Use delete() for models with ORM cascades. Override in subclasses
        when cascade behavior is required.
        """
        if not ids:
            return 0
        pk_col = self._get_pk_column()
        stmt = sa_delete(self.model_cls).where(pk_col.in_(ids))
        result: CursorResult[Any] = await self.session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount
