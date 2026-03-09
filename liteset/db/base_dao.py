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

from typing import Any, Generic, TypeVar

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

T = TypeVar("T", bound=DeclarativeBase)


class BaseAsyncDAO(Generic[T]):
    model_cls: type[T]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def find_by_id(self, model_id: int | str) -> T | None:
        return await self.session.get(self.model_cls, model_id)

    async def find_by_ids(self, model_ids: list[int | str]) -> list[T]:
        pk_cols = inspect(self.model_cls).primary_key
        if len(pk_cols) != 1:
            raise ValueError(
                f"{self.model_cls.__name__} has composite PK; "
                "use a custom query instead of find_by_ids"
            )
        pk_col = getattr(self.model_cls, pk_cols[0].name)
        stmt = select(self.model_cls).where(pk_col.in_(model_ids))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_all(self, filters: list[Any] | None = None) -> list[T]:
        stmt = select(self.model_cls)
        if filters:
            stmt = stmt.where(*filters)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

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
