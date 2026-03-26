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

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from liteset.models.core import FavStar, FavStarClassName

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class FavoriteMixin:
    """Mixin providing favorite CRUD for DAOs.

    Subclass must set ``_fav_class_name`` and have ``self.session`` (AsyncSession).
    """

    session: AsyncSession
    _fav_class_name: FavStarClassName

    async def favorited_ids(self, obj_ids: list[int], user_id: int) -> list[int]:
        """Return IDs of objects that the user has favorited."""
        if not obj_ids:
            return []
        stmt = select(FavStar.obj_id).where(
            FavStar.class_name == self._fav_class_name,
            FavStar.obj_id.in_(obj_ids),
            FavStar.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def is_favorited_by(self, obj_id: int, user_id: int) -> bool:
        """Check if a single object is favorited by the user."""
        return obj_id in await self.favorited_ids([obj_id], user_id)

    async def add_favorite(self, obj_id: int, user_id: int) -> None:
        """Add an object to user's favorites (idempotent)."""
        existing = await self.favorited_ids([obj_id], user_id)
        if existing:
            return
        fav = FavStar(
            class_name=self._fav_class_name,
            obj_id=obj_id,
            user_id=user_id,
            dttm=datetime.now(tz=timezone.utc),
        )
        self.session.add(fav)
        await self.session.flush()

    async def remove_favorite(self, obj_id: int, user_id: int) -> None:
        """Remove an object from user's favorites."""
        stmt = delete(FavStar).where(
            FavStar.class_name == self._fav_class_name,
            FavStar.obj_id == obj_id,
            FavStar.user_id == user_id,
        )
        await self.session.execute(stmt)
