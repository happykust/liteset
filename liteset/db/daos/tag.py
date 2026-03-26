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

from sqlalchemy import delete, select

from liteset.db.base_dao import BaseAsyncDAO
from liteset.models.tags import Tag, TaggedObject, user_favorite_tag_table


class AsyncTagDAO(BaseAsyncDAO[Tag]):
    model_cls = Tag

    async def find_by_name(self, name: str) -> Tag | None:
        """Find a tag by name."""
        return await self.find_one_or_none(name=name)

    async def find_by_names(self, names: list[str]) -> list[Tag]:
        """Find tags by a list of names."""
        if not names:
            return []
        stmt = select(Tag).where(Tag.name.in_(names))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_name(self, name: str, type_name: str = "custom") -> Tag:
        """Get tag by name, creating it if it doesn't exist."""
        from liteset.models.tags import TagType

        tag_type = TagType[type_name] if isinstance(type_name, str) else type_name
        tag = await self.find_one_or_none(name=name, type=tag_type)
        if tag:
            return tag
        tag = Tag(name=name, type=tag_type)
        self.session.add(tag)
        await self.session.flush()
        return tag

    async def create_custom_tagged_objects(
        self,
        object_type: str,
        object_id: int,
        tag_names: list[str],
    ) -> None:
        """Create TaggedObject entries for the given tag names."""
        from liteset.models.tags import ObjectType

        obj_type = ObjectType[object_type]
        for name in tag_names:
            tag = await self.get_by_name(name, "custom")
            tag_id: int = tag.id  # type: ignore[assignment]
            existing = await self._find_tagged_object(obj_type.name, object_id, tag_id)
            if not existing:
                tagged = TaggedObject(
                    tag_id=tag_id,
                    object_id=object_id,
                    object_type=obj_type,
                )
                self.session.add(tagged)

    async def _find_tagged_object(
        self,
        object_type: str,
        object_id: int,
        tag_id: int,
    ) -> TaggedObject | None:
        stmt = select(TaggedObject).where(
            TaggedObject.object_type == object_type,
            TaggedObject.object_id == object_id,
            TaggedObject.tag_id == tag_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_tagged_object(
        self,
        object_type: str,
        object_id: int,
        tag_id: int,
    ) -> TaggedObject | None:
        """Find a specific tagged object entry."""
        return await self._find_tagged_object(object_type, object_id, tag_id)

    async def delete_tagged_object(
        self,
        object_type: str,
        object_id: int,
        tag_name: str,
    ) -> None:
        """Delete a tagged object by tag name."""
        tag = await self.find_by_name(tag_name)
        if not tag:
            return
        stmt = delete(TaggedObject).where(
            TaggedObject.tag_id == tag.id,
            TaggedObject.object_type == object_type,
            TaggedObject.object_id == object_id,
        )
        await self.session.execute(stmt)

    async def delete_tags(self, tag_names: list[str]) -> None:
        """Delete tags and their tagged objects by names."""
        if not tag_names:
            return
        tags = await self.find_by_names(tag_names)
        tag_ids = [t.id for t in tags]
        if tag_ids:
            await self.session.execute(
                delete(TaggedObject).where(TaggedObject.tag_id.in_(tag_ids))
            )
            await self.session.execute(delete(Tag).where(Tag.id.in_(tag_ids)))

    async def get_tagged_objects_by_tag_names(
        self,
        tag_names: list[str],
        obj_types: list[str] | None = None,
    ) -> list[TaggedObject]:
        """Get tagged objects by tag names with optional type filter."""
        tags = await self.find_by_names(tag_names)
        tag_ids: list[int] = [t.id for t in tags]  # type: ignore[misc]
        return await self.get_tagged_objects_by_tag_ids(tag_ids, obj_types)

    async def create_tag_relationship(
        self,
        objects_to_tag: list[tuple[str, int]],
        tag: Tag,
    ) -> None:
        """Create TaggedObject entries linking objects to a tag."""
        tag_id: int = tag.id  # type: ignore[assignment]
        for obj_type, obj_id in objects_to_tag:
            existing = await self._find_tagged_object(obj_type, obj_id, tag_id)
            if not existing:
                tagged = TaggedObject(
                    tag_id=tag_id,
                    object_id=obj_id,
                    object_type=obj_type,
                )
                self.session.add(tagged)

    async def get_tagged_objects_by_tag_ids(
        self,
        tag_ids: list[int],
        obj_types: list[str] | None = None,
    ) -> list[TaggedObject]:
        """Get tagged objects filtered by tag IDs and optionally by type."""
        if not tag_ids:
            return []
        stmt = select(TaggedObject).where(TaggedObject.tag_id.in_(tag_ids))
        if obj_types:
            stmt = stmt.where(TaggedObject.object_type.in_(obj_types))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def favorite_tag_by_id_for_current_user(
        self,
        tag_id: int,
        user_id: int,
    ) -> bool:
        """Mark a tag as favorite for a user.

        Returns True if the tag was found and favorited.
        """
        tag = await self.find_by_id(tag_id)
        if not tag:
            return False

        # Check if already favorited to avoid duplicate insert
        existing = await self.favorited_ids([tag_id], user_id)
        if existing:
            return True

        stmt = user_favorite_tag_table.insert().values(
            tag_id=tag_id,
            user_id=user_id,
        )
        await self.session.execute(stmt)
        return True

    async def remove_user_favorite_tag(
        self,
        tag_id: int,
        user_id: int,
    ) -> bool:
        """Remove a tag from user's favorites.

        Returns True if the tag was found.
        """
        tag = await self.find_by_id(tag_id)
        if not tag:
            return False

        stmt = delete(user_favorite_tag_table).where(
            user_favorite_tag_table.c.tag_id == tag_id,
            user_favorite_tag_table.c.user_id == user_id,
        )
        await self.session.execute(stmt)
        return True

    async def favorited_ids(
        self,
        tag_ids: list[int],
        user_id: int,
    ) -> list[int]:
        """Return IDs of tags that the user has favorited."""
        if not tag_ids:
            return []
        stmt = select(user_favorite_tag_table.c.tag_id).where(
            user_favorite_tag_table.c.tag_id.in_(tag_ids),
            user_favorite_tag_table.c.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
