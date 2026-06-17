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
"""Utilities for Superset's ``init`` / CLI export commands.

* :func:`export_schema_to_dict` — emit the schema description for every
  exportable model (``{databases: [<schema dict>]}``).
* :func:`export_to_dict` — serialise every :class:`Database` row in the
  metadata DB to a plain dict (used by ``superset export-datasources``).

Both rely on :class:`superset.models.helpers.ImportExportMixin` whose
``export_to_dict`` / ``export_schema`` methods are pure-Python and
do not require an active session.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

EXPORT_VERSION = "1.0.0"
DATABASES_KEY = "databases"

logger = logging.getLogger(__name__)


def export_schema_to_dict(back_references: bool) -> dict[str, Any]:
    """Return the schema description for every exportable model.

    :class:`Database` is the only top-level exportable model in Superset,
    so the output is::

        {"databases": [<Database.export_schema() dict>]}
    """
    from superset.models.core import Database

    databases = [
        Database.export_schema(recursive=True, include_parent_ref=back_references)
    ]
    data: dict[str, Any] = {}
    if databases:
        data[DATABASES_KEY] = databases
    return data


def export_to_dict(
    recursive: bool,
    back_references: bool,
    include_defaults: bool,
) -> dict[str, Any]:
    """Serialise every :class:`Database` row to a plain dict.

    Opens a short-lived sync :class:`Session` via
    :func:`superset.utils.rls._metadata_sync_engine`, queries all
    :class:`Database` rows, and calls
    :meth:`ImportExportMixin.export_to_dict` on each to build the
    YAML-friendly dict.

    Callers running inside the ASGI request lifecycle should use
    :func:`export_to_dict_async` instead and pass their existing
    :class:`AsyncSession`.
    """
    from sqlalchemy.orm import Session

    from superset.models.core import Database
    from superset.utils.rls import _metadata_sync_engine

    logger.info("Starting export")
    engine = _metadata_sync_engine()
    with Session(engine) as session:
        db_rows = session.query(Database).order_by(Database.id).all()
        databases = [
            database.export_to_dict(
                recursive=recursive,
                include_parent_ref=back_references,
                include_defaults=include_defaults,
            )
            for database in db_rows
        ]
    logger.info("Exported %d %s", len(databases), DATABASES_KEY)
    data: dict[str, Any] = {}
    if databases:
        data[DATABASES_KEY] = databases
    return data


async def export_to_dict_async(
    session: AsyncSession,
    recursive: bool,
    back_references: bool,
    include_defaults: bool,
) -> dict[str, Any]:
    """Async equivalent of :func:`export_to_dict`.

    Callers from the ASGI request loop pass their bound
    :class:`AsyncSession`; we issue a single ``SELECT * FROM dbs`` and
    let :meth:`ImportExportMixin.export_to_dict` build the
    YAML-friendly dict for each row.
    """
    from superset.models.core import Database

    logger.info("Starting async export")
    stmt = select(Database).order_by(Database.id)
    result = await session.execute(stmt)
    db_rows = list(result.scalars().all())
    databases = [
        database.export_to_dict(
            recursive=recursive,
            include_parent_ref=back_references,
            include_defaults=include_defaults,
        )
        for database in db_rows
    ]
    logger.info("Exported %d %s", len(databases), DATABASES_KEY)
    data: dict[str, Any] = {}
    if databases:
        data[DATABASES_KEY] = databases
    return data


__all__ = [
    "DATABASES_KEY",
    "EXPORT_VERSION",
    "export_schema_to_dict",
    "export_to_dict",
    "export_to_dict_async",
]
