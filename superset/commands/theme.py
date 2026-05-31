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
"""Theme command classes — business logic for theme CRUD and system default."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import (
    CommandInvalidError,
    ForbiddenError,
    ObjectNotFoundError,
)

logger = logging.getLogger(__name__)


async def _validate_theme_deletable(theme: Any) -> None:
    """Block deletion of protected system themes — 1:1 with upstream
    ``DeleteThemeCommand.validate``: ``is_system`` → 403
    (``SystemThemeProtectedError``); ``is_system_default``/``is_system_dark``
    → 422 (``SystemThemeInUseError``)."""
    if getattr(theme, "is_system", False):
        raise ForbiddenError("System themes cannot be deleted.")
    if getattr(theme, "is_system_default", False) or getattr(
        theme, "is_system_dark", False
    ):
        raise CommandInvalidError(
            "Cannot delete a theme that is set as the system default or dark theme."
        )


async def _dissociate_dashboards_from_themes(session: Any, theme_ids: list[int]) -> None:
    """NULL out ``Dashboard.theme_id`` for the given themes before deleting them
    — 1:1 with upstream ``_dissociate_dashboards``. Without this, deleting a
    theme referenced by a dashboard violates the ``dashboards.theme_id`` FK
    (or leaves a dangling reference)."""
    if not theme_ids:
        return
    from sqlalchemy import update as _sa_update

    from superset.models.dashboard import Dashboard

    await session.execute(
        _sa_update(Dashboard)
        .where(Dashboard.theme_id.in_(theme_ids))
        .values(theme_id=None)
    )

if TYPE_CHECKING:
    from superset.db.daos.theme import AsyncThemeDAO


class CreateThemeCommand(AsyncBaseCommand[Any]):
    def __init__(
        self,
        dao: AsyncThemeDAO,
        data: dict[str, Any],
    ) -> None:
        self._dao = dao
        self._data = data

    async def validate(self) -> None:
        theme_name = self._data.get("theme_name")
        if not theme_name or not theme_name.strip():
            raise CommandInvalidError("theme_name is required")
        # ``json_data`` is required and must be a parseable JSON string —
        # mirrors original ThemeBaseSchema.validate_json_data at
        # superset_old/themes/schemas.py:70-80. We accept either a string
        # (raw JSON) or already-parsed dict.
        json_data = self._data.get("json_data")
        if json_data in (None, ""):
            raise CommandInvalidError("json_data is required")
        if isinstance(json_data, str):
            import json as _json

            try:
                _json.loads(json_data)
            except (TypeError, ValueError) as ex:
                raise CommandInvalidError("Invalid JSON configuration") from ex

    async def run(self) -> Any:
        item = await self._dao.create(self._data)
        await self._dao.session.flush()
        return item


class UpdateThemeCommand(AsyncBaseCommand[Any]):
    def __init__(
        self,
        dao: AsyncThemeDAO,
        pk: int,
        data: dict[str, Any],
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._pk)
        if not self._model:
            raise ObjectNotFoundError("Theme", self._pk)

    async def run(self) -> Any:
        item = await self._dao.update(self._model, self._data)
        await self._dao.session.flush()
        return item


class DeleteThemeCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncThemeDAO,
        pk: int,
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._pk)
        if not self._model:
            raise ObjectNotFoundError("Theme", self._pk)
        # System-theme protection (is_system→403, default/dark→422), 1:1 upstream.
        await _validate_theme_deletable(self._model)

    async def run(self) -> None:
        # Dissociate dashboards (NULL theme_id) before delete to preserve FK
        # integrity — 1:1 with upstream's ``_dissociate_dashboards``.
        await _dissociate_dashboards_from_themes(self._dao.session, [self._model.id])
        await self._dao.delete([self._model])
        await self._dao.session.flush()


class SetSystemDefaultCommand(AsyncBaseCommand[Any]):
    """Set a theme as the system default, unsetting the previous default."""

    def __init__(
        self,
        dao: AsyncThemeDAO,
        pk: int,
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._pk)
        if not self._model:
            raise ObjectNotFoundError("Theme", self._pk)

    async def run(self) -> Any:
        # Unset previous system default
        current_default = await self._dao.find_system_default()
        if current_default and current_default.id != self._pk:
            current_default.is_system_default = False

        self._model.is_system_default = True
        await self._dao.session.flush()
        return self._model


class UnsetSystemDefaultCommand(AsyncBaseCommand[None]):
    """Remove the system default flag from the current default theme."""

    def __init__(
        self,
        dao: AsyncThemeDAO,
    ) -> None:
        self._dao = dao

    async def validate(self) -> None:
        pass

    async def run(self) -> None:
        current_default = await self._dao.find_system_default()
        if current_default:
            current_default.is_system_default = False
            await self._dao.session.flush()


class BulkDeleteThemeCommand(AsyncBaseCommand[int]):
    """Bulk delete themes by IDs. Returns count of deleted themes."""

    def __init__(
        self,
        dao: AsyncThemeDAO,
        ids: list[int],
    ) -> None:
        self._dao = dao
        self._ids = ids
        self._models: list[Any] = []

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No theme IDs provided")
        # System-theme protection per theme + existence — 1:1 with upstream
        # ``DeleteThemeCommand.validate`` (the bulk path previously bypassed
        # ALL checks via a raw ``bulk_delete``).
        self._models = await self._dao.find_by_ids(self._ids)
        if not self._models or len(self._models) != len(self._ids):
            raise ObjectNotFoundError("Theme", str(self._ids))
        for theme in self._models:
            await _validate_theme_deletable(theme)

    async def run(self) -> int:
        # Dissociate dashboards before delete (FK integrity), then delete via
        # the ORM so cascades/events fire — not a raw bulk_delete.
        await _dissociate_dashboards_from_themes(self._dao.session, list(self._ids))
        count = len(self._models)
        await self._dao.delete(self._models)
        await self._dao.session.flush()
        return count


class SetSystemDarkCommand(AsyncBaseCommand[Any]):
    """Set a theme as the system dark theme, unsetting the previous one."""

    def __init__(
        self,
        dao: AsyncThemeDAO,
        pk: int,
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._pk)
        if not self._model:
            raise ObjectNotFoundError("Theme", self._pk)

    async def run(self) -> Any:
        current_dark = await self._dao.find_system_dark()
        if current_dark and current_dark.id != self._pk:
            current_dark.is_system_dark = False
        self._model.is_system_dark = True
        await self._dao.session.flush()
        return self._model


class UnsetSystemDarkCommand(AsyncBaseCommand[None]):
    """Remove the system dark flag from the current dark theme."""

    def __init__(
        self,
        dao: AsyncThemeDAO,
    ) -> None:
        self._dao = dao

    async def validate(self) -> None:
        pass

    async def run(self) -> None:
        current_dark = await self._dao.find_system_dark()
        if current_dark:
            current_dark.is_system_dark = False
            await self._dao.session.flush()


class ExportThemesCommand(AsyncBaseCommand[list[tuple[str, str]]]):
    """Export all themes as a list of (filename, yaml_content) tuples.

    Ported from superset_old/commands/theme/export.py.
    """

    def __init__(self, dao: AsyncThemeDAO) -> None:
        self._dao = dao

    async def validate(self) -> None:
        pass

    async def run(self) -> list[tuple[str, str]]:
        import yaml

        from superset.utils.file import get_filename

        themes = await self._dao.find_all()
        result: list[tuple[str, str]] = []
        for theme in themes:
            file_name = get_filename(theme.theme_name, theme.id, skip_id=True)
            payload = theme.export_to_dict(
                recursive=False,
                include_parent_ref=False,
                include_defaults=True,
                export_uuids=True,
            )
            # Parse json_data for readability (matching original)
            if payload.get("json_data"):
                try:
                    import json as _json

                    json_data = _json.loads(payload["json_data"])
                    payload["json_data"] = json_data
                except (TypeError, ValueError):
                    pass
            payload["version"] = "1.0.0"
            file_content = yaml.safe_dump(payload, sort_keys=False)
            result.append((f"themes/{file_name}.yaml", file_content))
        return result


class ImportThemesCommand(AsyncBaseCommand[int]):
    """Import themes from parsed YAML configs. Returns count of imported themes.

    Ported from superset_old/commands/theme/import_themes.py.
    """

    def __init__(
        self,
        dao: AsyncThemeDAO,
        contents: dict[str, Any],
        overwrite: bool = False,
    ) -> None:
        self._dao = dao
        self._contents = contents
        self._overwrite = overwrite

    async def validate(self) -> None:
        if not self._contents:
            raise CommandInvalidError("No theme contents provided")

    async def run(self) -> int:
        count = 0
        for file_name, config in self._contents.items():
            if not file_name.startswith("themes/"):
                continue
            if not isinstance(config, dict):
                continue

            # Convert json_data from dict to string if needed
            if isinstance(config.get("json_data"), dict):
                import json as _json

                config["json_data"] = _json.dumps(config["json_data"])

            uuid_val = config.get("uuid")
            existing = None
            if uuid_val:
                existing = await self._dao.find_by_uuid(uuid_val)

            if existing:
                if self._overwrite:
                    await self._dao.update(existing, config)
                # else skip
            else:
                await self._dao.create(config)
            count += 1

        await self._dao.session.flush()
        return count
