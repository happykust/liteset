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


_THEME_MODES = {"default", "dark", "system", "compact"}


def _is_valid_algorithm(alg: Any) -> bool:
    """Return True if ``alg`` is a valid ThemeMode value or list of ThemeMode values."""
    if isinstance(alg, str):
        return alg in _THEME_MODES
    if isinstance(alg, list):
        return all(isinstance(a, str) and a in _THEME_MODES for a in alg)
    return False


def _is_valid_theme(theme: Any) -> bool:
    """Validate a parsed theme ``json_data`` dict — 1:1 with
    ``superset_old/themes/utils.py::is_valid_theme``. An empty dict is valid;
    otherwise token/components must be dicts, hashed/inherit bools, and
    ``algorithm`` a ThemeMode str or list of ThemeMode strs."""
    try:
        if not isinstance(theme, dict):
            return False
        if not theme:
            return True
        for field, expected_type in (
            ("token", dict),
            ("components", dict),
            ("hashed", bool),
            ("inherit", bool),
        ):
            if field in theme and not isinstance(theme[field], expected_type):
                return False
        if "algorithm" in theme and not _is_valid_algorithm(theme["algorithm"]):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _validate_theme_json_data(json_data: Any) -> None:
    """Parse + structurally validate ``json_data`` — 1:1 with upstream
    ``ThemeBaseSchema.validate_json_data``: invalid JSON → "Invalid JSON
    configuration"; bad structure → "Invalid theme configuration structure"."""
    import json as _json

    if isinstance(json_data, str):
        try:
            parsed = _json.loads(json_data)
        except (TypeError, ValueError) as ex:
            raise CommandInvalidError("Invalid JSON configuration") from ex
    else:
        parsed = json_data
    if not _is_valid_theme(parsed):
        raise CommandInvalidError("Invalid theme configuration structure")


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


async def _dissociate_dashboards_from_themes(
    session: Any, theme_ids: list[int]
) -> None:
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
        # Structural validation (token/components/hashed/inherit/algorithm types)
        # — 1:1 with upstream ``validate_json_data``. The port previously only
        # checked JSON-parseability, accepting structurally-invalid themes.
        _validate_theme_json_data(json_data)

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
        # Block modification of system themes — 1:1 with upstream
        # ``superset_old/commands/theme/update.py::UpdateThemeCommand.validate``
        # which raises ``SystemThemeProtectedError`` when ``model.is_system`` is
        # set.  The port uses ``ForbiddenError`` (HTTP 403) consistently for
        # ``is_system`` guards (see ``_validate_theme_deletable`` above).
        if getattr(self._model, "is_system", False):
            raise ForbiddenError("System themes cannot be modified.")
        # Validate json_data structure when provided (1:1 upstream PUT path).
        json_data = self._data.get("json_data")
        if json_data not in (None, ""):
            _validate_theme_json_data(json_data)

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
        from sqlalchemy import update as _sa_update

        from superset.models.core import Theme

        # Clear ALL existing system defaults in a single bulk query — 1:1
        # with superset_old/commands/theme/set_system_theme.py:44-48.
        await self._dao.session.execute(
            _sa_update(Theme)
            .where(Theme.is_system_default.is_(True))
            .values(is_system_default=False)
        )

        # Set the new system default
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
        from sqlalchemy import update as _sa_update

        from superset.models.core import Theme

        # Clear ALL system default themes in a single bulk query — 1:1
        # with superset_old/commands/theme/set_system_theme.py:99-103.
        await self._dao.session.execute(
            _sa_update(Theme)
            .where(Theme.is_system_default.is_(True))
            .values(is_system_default=False)
        )
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
        from sqlalchemy import update as _sa_update

        from superset.models.core import Theme

        # Clear ALL existing system dark themes in a single bulk query — 1:1
        # with superset_old/commands/theme/set_system_theme.py:75-79.
        await self._dao.session.execute(
            _sa_update(Theme)
            .where(Theme.is_system_dark.is_(True))
            .values(is_system_dark=False)
        )

        # Set the new system dark theme
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
        from sqlalchemy import update as _sa_update

        from superset.models.core import Theme

        # Clear ALL system dark themes in a single bulk query — 1:1
        # with superset_old/commands/theme/set_system_theme.py:116-120.
        await self._dao.session.execute(
            _sa_update(Theme)
            .where(Theme.is_system_dark.is_(True))
            .values(is_system_dark=False)
        )
        await self._dao.session.flush()


def _validate_import_config(file_name: str, config: Any) -> list[str]:
    """Validate a single theme import config against ImportV1ThemeSchema rules.

    Returns a list of human-readable error strings (empty → valid).
    1:1 with ``ImportV1ThemeSchema`` (superset_old/themes/schemas.py:28-32):
    ``theme_name``, ``json_data``, ``uuid``, and ``version`` are all
    ``required=True``; ``uuid`` must also be a valid UUID-format string.
    """
    import uuid as _uuid_mod

    errors: list[str] = []
    # ``fields.String(required=True)`` accepts an EMPTY string — only a
    # missing key / explicit null is an error (superset_old/themes/schemas.py:
    # 29,32). ``uuid: ""`` is rejected anyway by the UUID-format check below
    # (``fields.UUID`` semantics).
    for field in ("theme_name", "uuid", "version"):
        if config.get(field) is None:
            errors.append(f"{file_name}: missing required field '{field}'")
    if config.get("json_data") is None:
        errors.append(f"{file_name}: missing required field 'json_data'")
    uuid_str = config.get("uuid")
    # ``is not None`` (not truthiness): an empty-string uuid must hit the
    # format check and fail like ``fields.UUID`` would.
    if uuid_str is not None and not errors:
        try:
            _uuid_mod.UUID(str(uuid_str))
        except (ValueError, AttributeError):
            errors.append(f"{file_name}: 'uuid' is not a valid UUID")
    return errors


class ExportThemesCommand(AsyncBaseCommand[list[tuple[str, str]]]):
    """Export all themes as a list of (filename, yaml_content) tuples.

    Ported from superset_old/commands/theme/export.py.
    """

    def __init__(self, dao: AsyncThemeDAO, model_ids: list[int] | None = None) -> None:
        self._dao = dao
        self._model_ids = model_ids
        self._models: list[Any] = []

    async def validate(self) -> None:
        # 1:1 with superset_old/commands/export/models.py:75-78:
        # ``if len(self._models) != len(self.model_ids): raise self.not_found()``
        if self._model_ids:
            self._models = await self._dao.find_by_ids(self._model_ids)
            if len(self._models) != len(self._model_ids):
                raise ObjectNotFoundError("Theme", str(self._model_ids))

    async def run(self) -> list[tuple[str, str]]:
        import yaml

        from superset.utils.file import get_filename

        # Use models pre-loaded during validate(); fall back to find_all()
        # when no specific IDs were requested.
        themes = self._models if self._models else await self._dao.find_all()
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
                    logger.info(
                        "Unable to decode `json_data` field: %s",
                        payload["json_data"],
                    )
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
        current_user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._contents = contents
        self._overwrite = overwrite
        self._current_user = current_user

    async def validate(self) -> None:
        # NB: empty ``contents`` is NOT an error — the original base
        # ImportModelsCommand.validate() only checks metadata.yaml (done by
        # the controller here) and load_configs() of themes/*.yaml; a bundle
        # with a valid metadata.yaml and zero theme files imports as a no-op
        # and returns HTTP 200 (superset_old/commands/importers/v1/__init__.py
        # :82-104). The completely-empty-ZIP 4xx is raised at the controller
        # level, mirroring superset_old/themes/api.py:545-547.

        # Per-config schema validation — 1:1 with ImportV1ThemeSchema applied
        # by load_configs() in the original ImportModelsCommand.validate()
        # (superset_old/commands/importers/v1/utils.py:199 + themes/schemas.py:28-32).
        # The original schema declares theme_name, json_data, uuid, and version
        # all as required=True.  A missing or invalid-format field → HTTP 422;
        # the liteset port previously skipped this entirely, causing:
        #   - missing uuid   → random UUID generated, overwrite-detection broken
        #   - missing theme_name → NULL row instead of 422
        #   - missing version   → silently ignored instead of 422
        #   - invalid UUID str  → BinaryUUID.process_bind_param crash → 500
        all_errors: list[str] = []
        for file_name, config in self._contents.items():
            if file_name.startswith("themes/") and isinstance(config, dict):
                all_errors.extend(_validate_import_config(file_name, config))
        if all_errors:
            raise CommandInvalidError("; ".join(all_errors))

        # Overwrite protection runs after schema validation — 1:1 with
        # _prevent_overwrite_existing_model() ordering in the original
        # (which executes AFTER load_configs() guarantees valid UUIDs).
        if not self._overwrite:
            # 1:1 with _prevent_overwrite_existing_model (superset_old/
            # commands/importers/v1/__init__.py:135-155): collect a conflict
            # per colliding file and report them ALL in one combined error,
            # not just the first.
            conflicts: list[str] = []
            for file_name, config in self._contents.items():
                if not file_name.startswith("themes/"):
                    continue
                if not isinstance(config, dict):
                    continue
                uuid_val = config.get("uuid")
                if uuid_val and await self._dao.find_by_uuid(uuid_val):
                    conflicts.append(
                        f"{file_name}: Theme already exists and "
                        "`overwrite=true` was not passed"
                    )
            if conflicts:
                raise CommandInvalidError("; ".join(conflicts))

    async def run(self) -> int:
        import json as _json

        count = 0
        for file_name, config in self._contents.items():
            if not file_name.startswith("themes/"):
                continue
            if not isinstance(config, dict):
                continue

            # Convert json_data from dict to string if needed
            if isinstance(config.get("json_data"), dict):
                config["json_data"] = _json.dumps(config["json_data"])

            # Structurally validate the imported theme — 1:1 with upstream's
            # ImportV1ThemeSchema (which runs is_valid_theme). An empty
            # string is NOT exempt: upstream's validator does
            # ``json.loads("")`` → JSONDecodeError → "Invalid JSON
            # configuration" (superset_old/themes/schemas.py:34-46).
            raw_json = config.get("json_data")
            if raw_json is not None:
                _validate_theme_json_data(raw_json)

            uuid_val = config.get("uuid")
            existing = None
            if uuid_val:
                existing = await self._dao.find_by_uuid(uuid_val)

            if existing:
                if self._overwrite:
                    await self._dao.update(existing, config)
                # else skip
            else:
                # Assign the importing user as owner (created_by) — 1:1 with
                # upstream's ``get_user()`` ownership on import.
                if self._current_user is not None:
                    config = {
                        **config,
                        "created_by_fk": self._current_user.id,
                        "changed_by_fk": self._current_user.id,
                    }
                await self._dao.create(config)
            count += 1

        await self._dao.session.flush()
        return count
