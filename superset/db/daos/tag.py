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

from typing import Any

from sqlalchemy import delete, select

from superset.db.base_dao import BaseAsyncDAO
from superset.models.tags import Tag, TaggedObject, user_favorite_tag_table


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
        """Get tag by name, creating it if it doesn't exist.

        1:1 with the original ``TagDAO.get_by_name``
        (superset_old/daos/tag.py:113-126), which on a cache miss delegates
        to ``superset.tags.models.get_tag`` — the savepoint-protected
        fetch-or-create. Reuse the async port at
        ``superset.tags.core.get_tag`` so a concurrent identical-name insert
        only rolls back the SAVEPOINT instead of poisoning the outer
        transaction with a UniqueViolation.
        """
        from superset.models.tags import TagType
        from superset.tags.core import get_tag

        tag_type = TagType[type_name] if isinstance(type_name, str) else type_name
        return await get_tag(name, self.session, tag_type)

    async def create_custom_tagged_objects(
        self,
        object_type: str,
        object_id: int,
        tag_names: list[str],
    ) -> None:
        """Create TaggedObject entries for the given tag names.

        Stores ``object_type`` as the Enum *name* (string) — the
        ``tagged_object.object_type`` column is VARCHAR after migration
        ``07f9a902af1b`` which dropped the Postgres ENUM constraint.
        """
        from superset.models.tags import ObjectType

        obj_type = ObjectType[object_type]
        # striping and de-dupping (mirrors superset_old/daos/tag.py)
        clean_tag_names: set[str] = {tag.strip() for tag in tag_names}
        for name in clean_tag_names:
            tag = await self.get_by_name(name, "custom")
            tag_id: int = tag.id  # type: ignore[assignment]
            existing = await self._find_tagged_object(obj_type.name, object_id, tag_id)
            if not existing:
                tagged = TaggedObject(
                    tag_id=tag_id,
                    object_id=object_id,
                    object_type=obj_type.name,
                )
                self.session.add(tagged)
        await self.session.flush()

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
        """Delete a tagged object by tag name.

        1:1 with the original validation flow of
        ``DeleteTaggedObjectCommand`` (superset_old/commands/tag/delete.py)
        + ``TagRestApi.delete_object`` (superset_old/tags/api.py:459-467):
        a missing tag (``TagNotFoundError``) or a missing tagged-object link
        (``TaggedObjectNotFoundError``) surfaces as **404**, not a silent
        no-op. The original ``TagDAO.delete_tagged_object`` raises
        ``NoResultFound`` for both cases.
        """
        from superset.exceptions import ObjectNotFoundError

        tag = await self.find_by_name(tag_name.strip())
        if not tag:
            raise ObjectNotFoundError("Tag", tag_name)
        tagged_object = await self._find_tagged_object(
            object_type, object_id, tag.id
        )
        if tagged_object is None:
            raise ObjectNotFoundError("TaggedObject", tag_name)
        await self.session.delete(tagged_object)
        await self.session.flush()

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
    ) -> list[dict[str, Any]]:
        """Get tagged objects by tag names with optional type filter.

        Returns the entity-shaped dicts produced by
        :meth:`get_tagged_objects_by_tag_ids` — see that method's docstring
        for the contract.
        """
        tags = await self.find_by_names(tag_names)
        tag_ids: list[int] = [t.id for t in tags]  # type: ignore[misc]
        return await self.get_tagged_objects_by_tag_ids(tag_ids, obj_types)

    async def create_tag_relationship(
        self,
        objects_to_tag: list[tuple[str, int]],
        tag: Tag,
        bulk_create: bool = False,
    ) -> None:
        """Reconcile TaggedObject entries linking objects to a tag.

        1:1 with ``superset_old/daos/tag.py::create_tag_relationship``: adds
        rows for objects not yet tagged and — unless ``bulk_create`` — DELETES
        rows for objects no longer in ``objects_to_tag`` (the diff against the
        current set). This is what makes tag EDIT (PUT) actually reconcile the
        tagged-object set instead of being a no-op.

        ``obj_type`` may arrive as either an :class:`ObjectType` enum or an
        already-stringified name; coerce to string so we never pass an enum to
        asyncpg (which would raise DataError).
        """
        from sqlalchemy import delete as _sa_delete, select as _sa_select

        from superset.models.tags import ObjectType

        tag_id: int = tag.id  # type: ignore[assignment]

        def _norm(t: Any) -> str:
            return t.name if isinstance(t, ObjectType) else str(t)

        # Current (type, id) pairs already tagged.
        rows = await self.session.execute(
            _sa_select(TaggedObject.object_type, TaggedObject.object_id).where(
                TaggedObject.tag_id == tag_id
            )
        )
        current: set[tuple[str, int]] = {(r[0], r[1]) for r in rows}
        updated: set[tuple[str, int]] = {
            (_norm(t), int(i)) for t, i in objects_to_tag
        }

        # Add new associations.
        for obj_type, obj_id in updated - current:
            self.session.add(
                TaggedObject(tag_id=tag_id, object_id=obj_id, object_type=obj_type)
            )

        # Delete removed associations (single create/update path only) — when
        # ``objects_to_tag`` is empty this removes ALL current ones, matching
        # upstream's ``current if not objects_to_tag else current - updated``.
        if not bulk_create:
            to_delete = current if not objects_to_tag else current - updated
            for obj_type, obj_id in to_delete:
                await self.session.execute(
                    _sa_delete(TaggedObject).where(
                        TaggedObject.tag_id == tag_id,
                        TaggedObject.object_type == obj_type,
                        TaggedObject.object_id == obj_id,
                    )
                )

    async def get_tagged_objects_by_tag_ids(  # noqa: C901
        self,
        tag_ids: list[int],
        obj_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get the *entities* tagged by the given tag ids.

        Mirrors ``superset_old/daos/tag.py::TagDAO.get_tagged_objects_by_tag_ids``:
        returns Dashboard / Chart / SavedQuery (and Dataset where wired)
        rows shaped as
        ``{id, type, name, url, changed_on, created_by, creator, tags,
        owners}`` — *not* the raw ``TaggedObject`` link rows. Frontend
        ``Tagged Objects`` page reads ``.type`` / ``.name`` / ``.url``
        directly and breaks when given link metadata.
        """
        if not tag_ids:
            return []
        from sqlalchemy.orm import selectinload

        stmt = select(TaggedObject).where(TaggedObject.tag_id.in_(tag_ids))
        if obj_types:
            # ``TaggedObject.object_type`` is an Enum column — pass the
            # enum *names* (already strings in obj_types) and SA coerces.
            stmt = stmt.where(TaggedObject.object_type.in_(obj_types))
        result = await self.session.execute(stmt)
        links = list(result.scalars().all())

        # Group by type, then bulk-load each entity class once.
        from collections import defaultdict

        by_type: dict[str, list[int]] = defaultdict(list)
        for link in links:
            # Enum or already-str depending on dialect quirks; coerce.
            t = link.object_type
            t_name = getattr(t, "name", str(t)).rsplit(".", 1)[-1]
            by_type[t_name].append(link.object_id)

        results: list[dict[str, Any]] = []

        async def _load(ids: list[int], model: Any) -> list[Any]:
            if not ids:
                return []
            q = await self.session.execute(
                select(model)
                .where(model.id.in_(ids))
                .options(selectinload(model.owners), selectinload(model.tags))  # type: ignore[attr-defined]
            )
            return list(q.scalars().all())

        # Dashboards
        if not obj_types or "dashboard" in obj_types:
            from superset.models.dashboard import Dashboard

            for d in await _load(by_type.get("dashboard", []), Dashboard):
                results.append(
                    {
                        "id": d.id,
                        "type": "dashboard",
                        "name": getattr(d, "dashboard_title", None),
                        "url": getattr(d, "url", None),
                        "changed_on": getattr(d, "changed_on", None),
                        "created_by": getattr(d, "created_by_fk", None),
                        "creator": d.creator() if hasattr(d, "creator") else None,
                        "tags": [t.name for t in getattr(d, "tags", []) or []],
                        "owners": [o.id for o in getattr(d, "owners", []) or []],
                    }
                )

        # Charts (Slice)
        if not obj_types or "chart" in obj_types:
            from superset.models.slice import Slice

            for c in await _load(by_type.get("chart", []), Slice):
                results.append(
                    {
                        "id": c.id,
                        "type": "chart",
                        "name": getattr(c, "slice_name", None),
                        "url": getattr(c, "url", None),
                        "changed_on": getattr(c, "changed_on", None),
                        "created_by": getattr(c, "created_by_fk", None),
                        "creator": c.creator() if hasattr(c, "creator") else None,
                        "tags": [t.name for t in getattr(c, "tags", []) or []],
                        "owners": [o.id for o in getattr(c, "owners", []) or []],
                    }
                )

        # Saved queries
        if (not obj_types or "query" in obj_types) and by_type.get("query"):
            sq_ids = by_type["query"]
            try:
                from superset.models.sql_lab import (
                    SavedQuery as _SavedQuery,  # noqa: N813
                )
            except ImportError:
                _SavedQuery = None  # type: ignore[assignment]  # noqa: N806
            if _SavedQuery is not None:
                q = await self.session.execute(
                    select(_SavedQuery).where(_SavedQuery.id.in_(sq_ids))
                )
                for sq in q.scalars().all():
                    results.append(
                        {
                            "id": sq.id,
                            "type": "query",
                            "name": getattr(sq, "label", None),
                            "url": (
                                sq.url() if callable(getattr(sq, "url", None))
                                else getattr(sq, "url", None)
                            ),
                            "changed_on": getattr(sq, "changed_on", None),
                            "created_by": getattr(sq, "created_by_fk", None),
                            "creator": (
                                sq.creator() if hasattr(sq, "creator") else None
                            ),
                            "tags": [],
                            "owners": [],
                        }
                    )

        return results

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
