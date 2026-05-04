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
# mypy: ignore-errors
"""Async tag helpers -- ported 1:1 from superset_old/tags/models.py and
superset_old/tags/core.py.

The synchronous ORM event listeners (``ObjectUpdater``, ``ChartUpdater``,
etc.) cannot be used in async sessions.  Instead, the equivalent logic is
exposed as async helper functions that commands call explicitly after
insert/update/delete.

Functions:
    get_tag          -- fetch-or-create a tag (async port of models.get_tag)
    get_object_type  -- map class name to ObjectType enum (pure, no I/O)
    add_implicit_tags_after_insert  -- create ``type:`` and ``owner:`` tags
    sync_owner_tags_after_update    -- reconcile ``owner:`` tags
    delete_tagged_objects           -- remove all tagged_object rows
    add_favorited_by_tag            -- add ``favorited_by:`` tag
    remove_favorited_by_tag         -- remove ``favorited_by:`` tag
"""

from __future__ import annotations

import logging

from sqlalchemy import delete as sa_delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from superset.models.tags import ObjectType, Tag, TaggedObject, TagType

logger = logging.getLogger(__name__)


async def get_tag(
    name: str,
    session: AsyncSession,
    type_: TagType,
) -> Tag:
    """Fetch an existing tag by *name* + *type*, or create one if it does not
    exist.

    Port of ``superset_old/tags/models.py::get_tag`` -- async version.

    :param name: Tag name (will be stripped of leading/trailing whitespace).
    :param session: An active :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
    :param type_: The :class:`TagType` for the tag.
    :returns: The existing or newly created :class:`Tag`.
    """
    tag_name = name.strip()
    stmt = select(Tag).where(Tag.name == tag_name, Tag.type == type_)
    result = await session.execute(stmt)
    tag = result.scalars().one_or_none()
    if tag is None:
        tag = Tag(name=tag_name, type=type_)
        session.add(tag)
        await session.flush()
    return tag


def get_object_type(class_name: str) -> ObjectType:
    """Map a class name (case-insensitive) to an :class:`ObjectType`.

    Port of ``superset_old/tags/models.py::get_object_type`` -- kept
    synchronous because it is a pure mapping with no I/O.

    :param class_name: One of ``"slice"``, ``"dashboard"``, ``"query"``,
        ``"dataset"`` (case-insensitive).
    :returns: The corresponding :class:`ObjectType` enum member.
    :raises ValueError: If *class_name* does not match any known type.
    """
    mapping: dict[str, ObjectType] = {
        "slice": ObjectType.chart,
        "dashboard": ObjectType.dashboard,
        "query": ObjectType.query,
        "dataset": ObjectType.dataset,
    }
    try:
        return mapping[class_name.lower()]
    except KeyError as ex:
        raise ValueError(f"No mapping found for {class_name}") from ex


# ---------------------------------------------------------------------------
# Async equivalents of ObjectUpdater.after_insert / after_update / after_delete
# ---------------------------------------------------------------------------


async def _add_tag_object_if_not_tagged(
    session: AsyncSession,
    tag_id: int,
    object_id: int,
    object_type: str,
) -> None:
    """Add a ``TaggedObject`` row if one does not already exist.

    Port of ``ObjectUpdater.add_tag_object_if_not_tagged``.
    """
    exists_query = exists().where(
        TaggedObject.tag_id == tag_id,
        TaggedObject.object_id == object_id,
        TaggedObject.object_type == object_type,
    )
    result = await session.execute(select(exists_query))
    already_tagged: bool = result.scalar()  # type: ignore[assignment]
    if not already_tagged:
        tagged_object = TaggedObject(
            tag_id=tag_id, object_id=object_id, object_type=object_type
        )
        session.add(tagged_object)


async def add_implicit_tags_after_insert(
    session: AsyncSession,
    object_type: str,
    object_id: int,
    owner_ids: list[int],
) -> None:
    """Create implicit ``type:`` and ``owner:`` tags after an object is created.

    Async port of ``ObjectUpdater.after_insert``.

    :param session: Active async session (will be flushed but not committed).
    :param object_type: One of ``"chart"``, ``"dashboard"``, ``"query"``,
        ``"dataset"``.
    :param object_id: The newly created object's primary key.
    :param owner_ids: List of user IDs that own the object.
    """
    # Add owner tags
    for owner_id in owner_ids:
        tag = await get_tag(f"owner:{owner_id}", session, TagType.owner)
        await _add_tag_object_if_not_tagged(
            session, tag_id=tag.id, object_id=object_id, object_type=object_type
        )

    # Add type tag
    tag = await get_tag(f"type:{object_type}", session, TagType.type)
    await _add_tag_object_if_not_tagged(
        session, tag_id=tag.id, object_id=object_id, object_type=object_type
    )
    await session.flush()


async def sync_owner_tags_after_update(
    session: AsyncSession,
    object_type: str,
    object_id: int,
    owner_ids: list[int],
) -> None:
    """Reconcile ``owner:`` tags after an object is updated.

    Async port of ``ObjectUpdater.after_update``.  Adds missing owner tags
    and removes stale ones.

    :param session: Active async session.
    :param object_type: Object type string.
    :param object_id: The object's primary key.
    :param owner_ids: Current list of owner user IDs.
    """
    # Fetch existing owner tagged_objects for this object
    stmt = (
        select(TaggedObject)
        .join(Tag)
        .where(
            TaggedObject.object_type == object_type,
            TaggedObject.object_id == object_id,
            Tag.type == TagType.owner,
        )
    )
    result = await session.execute(stmt)
    existing_tags = list(result.scalars().all())
    existing_owner_tag_ids = {t.tag_id for t in existing_tags}

    # Determine new owner tag IDs
    new_owner_tag_ids: set[int] = set()
    for owner_id in owner_ids:
        tag = await get_tag(f"owner:{owner_id}", session, TagType.owner)
        new_owner_tag_ids.add(tag.id)

    # Add missing tags
    for owner_tag_id in new_owner_tag_ids - existing_owner_tag_ids:
        tagged_object = TaggedObject(
            tag_id=owner_tag_id,
            object_id=object_id,
            object_type=object_type,
        )
        session.add(tagged_object)

    # Remove stale tags
    for tagged_obj in existing_tags:
        if tagged_obj.tag_id not in new_owner_tag_ids:
            await session.delete(tagged_obj)

    await session.flush()


async def delete_tagged_objects(
    session: AsyncSession,
    object_type: str,
    object_id: int,
) -> None:
    """Remove all ``tagged_object`` rows for a deleted object.

    Async port of ``ObjectUpdater.after_delete``.

    :param session: Active async session.
    :param object_type: Object type string.
    :param object_id: The deleted object's primary key.
    """
    stmt = sa_delete(TaggedObject).where(
        TaggedObject.object_type == object_type,
        TaggedObject.object_id == object_id,
    )
    await session.execute(stmt)
    await session.flush()


async def add_favorited_by_tag(
    session: AsyncSession,
    object_type: str,
    object_id: int,
    user_id: int,
) -> None:
    """Add a ``favorited_by:`` tag for a user.

    Async port of ``FavStarUpdater.after_insert``.

    :param session: Active async session.
    :param object_type: Object type string (e.g. ``"chart"``).
    :param object_id: The favorited object's primary key.
    :param user_id: The user who favorited the object.
    """
    tag = await get_tag(f"favorited_by:{user_id}", session, TagType.favorited_by)
    tagged_object = TaggedObject(
        tag_id=tag.id,
        object_id=object_id,
        object_type=object_type,
    )
    session.add(tagged_object)
    await session.flush()


async def remove_favorited_by_tag(
    session: AsyncSession,
    object_type: str,
    object_id: int,
    user_id: int,
) -> None:
    """Remove a ``favorited_by:`` tag for a user.

    Async port of ``FavStarUpdater.after_delete``.

    :param session: Active async session.
    :param object_type: Object type string.
    :param object_id: The un-favorited object's primary key.
    :param user_id: The user who removed the favorite.
    """
    name = f"favorited_by:{user_id}"
    stmt = (
        select(TaggedObject.id)
        .join(Tag)
        .where(
            TaggedObject.object_id == object_id,
            Tag.type == TagType.favorited_by,
            Tag.name == name,
        )
    )
    result = await session.execute(stmt)
    ids = [row[0] for row in result]
    if ids:
        await session.execute(sa_delete(TaggedObject).where(TaggedObject.id.in_(ids)))
        await session.flush()


def register_sqla_event_listeners() -> None:
    """Register synchronous SQLAlchemy event listeners for the tagging system.

    Port of ``superset_old/tags/core.py::register_sqla_event_listeners``.
    Delegates to :func:`superset.models._listeners.register` which holds
    the full listener wiring logic (slice perms, database PVM sync, user
    welcome-dashboard clone, and all tag updaters).

    Called from :func:`superset.app.on_startup` when the ``TAGGING_SYSTEM``
    feature flag is enabled.  Also called from
    :mod:`superset.models.__init__` so that import-time consumers (CLI,
    Alembic) also get listeners wired in.
    """
    from superset.models._listeners import register  # noqa: PLC0415

    register()
