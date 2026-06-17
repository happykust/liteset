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
"""Utilities for importing saved queries from a v1 bundle."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING
from uuid import UUID as _UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.models.sql_lab import SavedQuery

logger = logging.getLogger(__name__)


async def import_saved_query(
    session: AsyncSession,
    config: dict[str, Any],
    overwrite: bool = False,
) -> SavedQuery:
    """Upsert a saved query from a bundle config dict.

    UUID-based dedup: updates an existing row if the UUID matches, otherwise
    creates a new one. Strips non-model fields (``uuid``, ``version``,
    ``database_uuid``) before constructing the row.
    """
    from sqlalchemy import select as sa_select

    from superset.models.sql_lab import SavedQuery

    cfg = dict(config)
    uuid_str = cfg.get("uuid")
    existing: SavedQuery | None = None
    if uuid_str:
        existing = (
            (
                await session.execute(
                    sa_select(SavedQuery).where(SavedQuery.uuid == _UUID(str(uuid_str)))
                )
            )
            .scalars()
            .one_or_none()
        )

    if existing:
        if not overwrite:
            return existing
        cfg["id"] = existing.id

    cfg.pop("id", None)
    cfg.pop("version", None)
    cfg.pop("database_uuid", None)
    cfg.pop("uuid", None)

    attrs = {
        "label",
        "description",
        "schema",
        "catalog",
        "sql",
        "db_id",
        "rows",
        "tab_state_id",
        "extra_json",
        "is_managed_externally",
        "external_url",
    }
    filtered = {k: v for k, v in cfg.items() if k in attrs}

    if existing:
        for key, value in filtered.items():
            setattr(existing, key, value)
        saved_query = existing
    else:
        saved_query = SavedQuery(**filtered)
        if uuid_str:
            saved_query.uuid = _UUID(str(uuid_str))  # type: ignore[assignment]
        session.add(saved_query)

    if saved_query.id is None:
        await session.flush()

    return saved_query


__all__ = ["import_saved_query"]
