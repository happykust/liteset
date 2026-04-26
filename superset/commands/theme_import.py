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
"""Async port of ``superset_old/commands/theme/import_themes.py``.

The original lived under ``commands/theme/`` (a package); liteset
keeps the per-resource theme commands in a single ``commands/theme.py``
module.  We expose ``import_theme`` here so the dashboard import
pipeline can wire it via the canonical name described in the design
notes.

The ``superset.commands.theme.importers.v1.utils`` import path used by
the dashboard importer is wired through this re-export module — see
:mod:`superset.commands.theme` package shim once the theme command
module is converted to a package.  Until then importers directly
``from superset.commands.theme_import import import_theme``.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, TYPE_CHECKING
from uuid import UUID as _UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.models.core import Theme

logger = logging.getLogger(__name__)


async def import_theme(  # noqa: C901  # complex business logic
    session: AsyncSession,
    config: dict[str, Any],
    overwrite: bool = False,
    ignore_permissions: bool = True,
    security_manager: Any | None = None,
    current_user: Any | None = None,
) -> Theme | None:
    """Async 1:1 port of ``superset_old.commands.theme.import_themes.import_theme``.

    Handles UUID-based dedup, ``json_data`` dict-to-string serialisation,
    and owner attribution on creation.
    """
    from sqlalchemy import select as sa_select

    from superset.models.core import Theme

    can_write = ignore_permissions
    if not can_write and security_manager is not None:
        can_write = await security_manager.can_access("can_write", "Theme")

    cfg = dict(config)
    uuid_str = cfg.get("uuid")
    existing: Theme | None = None
    if uuid_str:
        existing = (
            (
                await session.execute(
                    sa_select(Theme).where(Theme.uuid == _UUID(uuid_str))
                )
            )
            .scalars()
            .one_or_none()
        )

    if existing:
        if not overwrite or not can_write:
            return existing
        cfg["id"] = existing.id
    elif not can_write:
        # Mirrors the original ``ThemeImportError`` — surface as plain
        # ``ImportFailedError`` so the orchestrator surfaces it 1:1.
        from superset.exceptions import ImportFailedError

        raise ImportFailedError(
            "Theme doesn't exist and user doesn't have permission to create themes"
        )

    if isinstance(cfg.get("json_data"), dict):
        cfg["json_data"] = _json.dumps(cfg["json_data"])

    cfg.pop("id", None)
    cfg.pop("version", None)
    cfg.pop("uuid", None)

    attrs = {"theme_name", "json_data"}
    filtered = {k: v for k, v in cfg.items() if k in attrs}

    if existing:
        for key, value in filtered.items():
            setattr(existing, key, value)
        theme = existing
    else:
        theme = Theme(**filtered)
        if uuid_str:
            theme.uuid = _UUID(uuid_str)  # type: ignore[assignment]
        session.add(theme)
    if theme.id is None:
        await session.flush()

    # Add current user as owner / changer on creation.
    if not existing and current_user is not None:
        if hasattr(theme, "changed_by"):
            theme.changed_by = current_user
        if hasattr(theme, "created_by"):
            theme.created_by = current_user

    return theme


__all__ = ["import_theme"]
