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
"""Async tag helpers -- ported 1:1 from superset_old/tags/models.py (get_tag,
get_object_type) and superset_old/tags/core.py (register/clear listeners).

The synchronous ORM event listeners (ObjectUpdater, ChartUpdater, etc.) are
intentionally NOT ported here because Litestar uses async sessions; those
updaters rely on synchronous ``sessionmaker(bind=connection)`` patterns that
do not translate to async.  The async equivalents are handled by the
``AsyncTagDAO`` in ``superset.db.daos.tag``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from superset.models.tags import ObjectType, Tag, TagType


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
