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
"""Tag command classes."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.tag import AsyncTagDAO

logger = logging.getLogger(__name__)


async def _resolve_tagged_object(session: Any, object_type: str, object_id: int) -> Any:
    """Resolve a chart/dashboard/query by id (owners eager-loaded for the
    ownership check). Returns ``None`` for types without an ownership model
    (e.g. dataset).
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    if object_type == "dashboard":
        from superset.models.dashboard import Dashboard

        model_cls: type[Any] = Dashboard
    elif object_type == "chart":
        from superset.models.slice import Slice

        model_cls = Slice
    elif object_type == "query":
        from superset.models.sql_lab import SavedQuery

        model_cls = SavedQuery
    else:
        return None
    stmt = select(model_cls).where(model_cls.id == object_id)
    if hasattr(model_cls, "owners"):
        stmt = stmt.options(selectinload(model_cls.owners))
    result = await session.execute(stmt)
    return result.scalars().one_or_none()


async def _skip_unowned_objects(
    session: Any,
    security_manager: Any,
    current_user: Any,
    objects_to_tag: list[Any],
) -> set[tuple[str, int]]:
    """Return the ``(object_type, object_id)`` pairs the user may NOT tag.

    Skips an object when ``raise_for_ownership`` denies AND the user isn't its
    ``created_by``. Admins own everything → nothing skipped. No security
    manager (e.g. internal callers) → skip nothing.
    """
    skipped: set[tuple[str, int]] = set()
    if security_manager is None:
        return skipped
    from superset.exceptions import SupersetSecurityException

    user_id = getattr(current_user, "id", None)
    exceptions: list[Exception] = []
    for obj in objects_to_tag:
        if not isinstance(obj, (list, tuple)) or len(obj) != 2:
            continue
        obj_type, obj_id = str(obj[0]), int(obj[1])
        try:
            model = await _resolve_tagged_object(session, obj_type, obj_id)
            if model is None:
                continue
            try:
                await security_manager.raise_for_ownership(model, user_id)
            except SupersetSecurityException:
                created_by_fk = getattr(model, "created_by_fk", None)
                if not created_by_fk or created_by_fk != user_id:
                    skipped.add((obj_type, obj_id))
        except Exception as ex:  # noqa: BLE001
            logger.debug("Failed to resolve tagged object", exc_info=True)
            exceptions.append(CommandInvalidError(str(ex)))
    if exceptions:
        raise CommandInvalidError(
            message="Tag parameters are invalid.", exceptions=exceptions
        )
    return skipped


class CreateTagCommand(AsyncBaseCommand[Any]):
    def __init__(
        self,
        dao: "AsyncTagDAO",
        data: dict[str, Any],
        security_manager: Any | None = None,
        current_user: Any | None = None,
        bulk_create: bool = False,
    ) -> None:
        self._dao = dao
        self._data = data
        self._security_manager = security_manager
        self._current_user = current_user
        self._bulk_create = bulk_create

    async def validate(self) -> None:
        name = self._data.get("name", "").strip()
        if not name:
            raise CommandInvalidError("name is required")

    async def run(self) -> Any:
        name = self._data["name"]
        # Re-submitting an existing tag with no description clears it (not a no-op).
        tag = await self._dao.get_by_name(name, "custom")
        tag.description = self._data.get("description", "")
        # Each entry is a [object_type, object_id] pair (not a dict).
        from superset.models.tags import ObjectType

        objects_to_tag = self._data.get("objects_to_tag", []) or []

        normalized: list[Any] = []
        for obj in objects_to_tag:
            if not isinstance(obj, (list, tuple)) or len(obj) != 2:
                raise CommandInvalidError(
                    f"Invalid objects_to_tag entry: {obj!r} "
                    "(expected [object_type, object_id])"
                )
            normalized.append(obj)
        skipped = await _skip_unowned_objects(
            self._dao.session,
            self._security_manager,
            self._current_user,
            [(str(o[0]), int(o[1])) for o in normalized],
        )

        pairs: list[tuple[str, int]] = []
        for obj in normalized:
            object_type, object_id = obj[0], obj[1]
            pair = (str(object_type), int(object_id))
            # ``ObjectType[unknown_type]`` would KeyError → 500; validate here for 4xx.
            if str(object_type) not in ObjectType.__members__:
                raise CommandInvalidError(f"invalid object type {object_type}")
            if pair in skipped:
                continue
            pairs.append(pair)

        # With bulk_create=False: absent associations are deleted (full replace);
        # bulk_create=True is insert-only.
        await self._dao.create_tag_relationship(
            pairs, tag, bulk_create=self._bulk_create
        )
        await self._dao.session.flush()
        return tag


class UpdateTagCommand(AsyncBaseCommand[Any]):
    def __init__(self, dao: AsyncTagDAO, pk: int, data: dict[str, Any]) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._item: Any = None

    async def validate(self) -> None:
        self._item = await self._dao.find_by_id(self._pk)
        if self._item is None:
            raise ObjectNotFoundError("Tag", self._pk)

        if objects_to_tag := self._data.get("objects_to_tag"):
            from superset.models.tags import ObjectType

            exceptions: list[Exception] = []
            for obj in objects_to_tag:
                if not isinstance(obj, (list, tuple)) or len(obj) != 2:
                    exceptions.append(
                        CommandInvalidError(
                            f"Invalid objects_to_tag entry: {obj!r} "
                            "(expected [object_type, object_id])"
                        )
                    )
                    continue
                obj_type = str(obj[0])
                if obj_type not in ObjectType.__members__:
                    exceptions.append(
                        CommandInvalidError(f"invalid object type {obj_type}")
                    )
            if exceptions:
                raise CommandInvalidError(
                    message="Tag parameters are invalid.", exceptions=exceptions
                )

    async def run(self) -> Any:
        # ``objects_to_tag`` is not a model column; passing it to dao.update
        # is a silent no-op.
        objects_to_tag = self._data.get("objects_to_tag", [])
        scalar_data = {k: v for k, v in self._data.items() if k != "objects_to_tag"}
        # Re-inject description so DAO always writes it (None = clear);
        # controller strips UNSET fields.
        scalar_data["description"] = self._data.get("description")
        item = await self._dao.update(self._item, scalar_data)

        pairs = [
            (str(o[0]), int(o[1]))
            for o in objects_to_tag
            if isinstance(o, (list, tuple)) and len(o) == 2
        ]
        await self._dao.create_tag_relationship(pairs, item, bulk_create=False)
        await self._dao.session.flush()
        return item


class DeleteTagCommand(AsyncBaseCommand[None]):
    def __init__(self, dao: AsyncTagDAO, pk: int) -> None:
        self._dao = dao
        self._pk = pk
        self._item: Any = None

    async def validate(self) -> None:
        self._item = await self._dao.find_by_id(self._pk)
        if self._item is None:
            raise ObjectNotFoundError("Tag", self._pk)

    async def run(self) -> None:
        await self._dao.delete([self._item])
        await self._dao.session.flush()


class BulkDeleteTagCommand(AsyncBaseCommand[int]):
    """Bulk delete tags by name.

    Receives a list of tag *names*, validates each exists, then delegates to
    ``TagDAO.delete_tags`` which removes both the tag rows and the
    associated ``tagged_object`` entries.
    """

    def __init__(self, dao: AsyncTagDAO, tag_names: list[str]) -> None:
        self._dao = dao
        self._tag_names = tag_names

    async def validate(self) -> None:
        if not self._tag_names:
            raise CommandInvalidError("No tag names provided for bulk delete")
        exceptions: list[Exception] = []
        for name in self._tag_names:
            if not await self._dao.find_by_name(name):
                exceptions.append(ObjectNotFoundError("Tag", name))
        if exceptions:
            raise CommandInvalidError(
                message=f"Invalid tag parameters: {self._tag_names}",
                exceptions=exceptions,
            )

    async def run(self) -> int:
        await self._dao.delete_tags(self._tag_names)
        await self._dao.session.flush()
        return len(self._tag_names)


class BulkCreateTagCommand(AsyncBaseCommand[dict[str, list[Any]]]):
    def __init__(
        self,
        dao: AsyncTagDAO,
        tags_data: list[dict[str, Any]],
        security_manager: Any | None = None,
        current_user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._tags_data = tags_data
        self._security_manager = security_manager
        self._current_user = current_user

    async def validate(self) -> None:
        for tag_data in self._tags_data:
            if not tag_data.get("name", "").strip():
                raise CommandInvalidError("All tags must have a name")

    async def run(self) -> dict[str, list[Any]]:
        """Create each custom tag + its relationships.

        Returns ``{objects_tagged, objects_skipped}`` — the ownership-skipped
        ``(type, id)`` pairs are collected across all tags so the frontend
        ``BulkTagModal`` can display what was and wasn't tagged.
        """
        all_tagged: set[tuple[str, int]] = set()
        all_skipped: set[tuple[str, int]] = set()
        for tag_data in self._tags_data:
            objects_to_tag = [
                (str(o[0]), int(o[1]))
                for o in (tag_data.get("objects_to_tag") or [])
                if isinstance(o, (list, tuple)) and len(o) == 2
            ]
            skipped = await _skip_unowned_objects(
                self._dao.session,
                self._security_manager,
                self._current_user,
                objects_to_tag,
            )
            all_skipped |= skipped
            all_tagged |= set(objects_to_tag)
            tag_data = {
                **tag_data,
                "objects_to_tag": [list(p) for p in objects_to_tag if p not in skipped],
            }
            await CreateTagCommand(
                dao=self._dao, data=tag_data, bulk_create=True
            ).execute()
        return {
            "objects_tagged": [list(p) for p in (all_tagged - all_skipped)],
            "objects_skipped": [list(p) for p in all_skipped],
        }
