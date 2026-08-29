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
"""Regression: ``POST /api/v1/assets/export`` (AsyncFullAssetManager) must
not bypass object-level access filters.

``AsyncFullAssetManager._export_type`` used to enumerate every row's id in
the table directly (``select(model_cls.id)``, no filter) and call the
export command's *private* ``_export_single`` for each — so ``validate()``
(and its ``_validate_access`` object-level check) never ran. Any holder of
``can_mulexport`` got every database/dataset/chart/dashboard/saved-query in
the deployment, including other users' (and Admins') saved-query SQL.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def two_databases():
    """In-memory SQLite session with two Database rows."""
    # ``_export_single`` calls ``dao.get_ssh_tunnel`` on every export, so
    # without registering this model the export fails with "no such table:
    # ssh_tunnels" and is silently swallowed to an empty result whenever
    # this test runs without some other, unrelated test module having
    # imported the model first, making its outcome depend on collection
    # order.
    import superset.models  # noqa: F401  (register models)
    import superset.models.ssh_tunnel  # noqa: F401  (register ssh_tunnels table)
    from superset.models.core import Database
    from superset.models.helpers import Base

    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        visible = Database(database_name="visible_db", sqlalchemy_uri="sqlite://")
        hidden = Database(database_name="hidden_db", sqlalchemy_uri="sqlite://")
        session.add_all([visible, hidden])
        await session.commit()
        yield session, visible, hidden
    await engine.dispose()


async def test_export_type_only_includes_accessible_ids(two_databases) -> None:
    from superset.importexport.manager import AsyncFullAssetManager

    session, visible, hidden = two_databases
    manager = AsyncFullAssetManager(session)
    sm = AsyncMock()
    user = SimpleNamespace(id=1)

    # Restrict to just the "visible" row's id — mirrors what
    # ``database_access_filters`` would produce for a non-admin holding a
    # per-database grant on only one of the two connections.
    from superset.models.core import Database

    async def _only_visible(_sm, _user):
        return [Database.id == visible.id]

    with patch(
        "superset.db.filters.database_access_filters",
        new=_only_visible,
    ):
        items = await manager._export_type("databases", sm, user)

    # Exactly the accessible database's own record must come back -- not
    # merely a non-empty result or a filename substring match, either of
    # which an unrelated or entirely-empty export could also satisfy.
    assert len(items) == 1
    _filename, content = items[0]
    exported = yaml.safe_load(content)
    assert exported["uuid"] == str(visible.uuid)
    assert exported["database_name"] == visible.database_name


async def test_export_type_unfiltered_without_security_manager(two_databases) -> None:
    """No security context (CLI/background export) stays permissive.

    Matches every other "no security context" fallback in this codebase
    (``AsyncExportModelsCommand._validate_access``, ``filter_visible_ids``).
    """
    from superset.importexport.manager import AsyncFullAssetManager

    session, visible, hidden = two_databases
    manager = AsyncFullAssetManager(session)

    items = await manager._export_type("databases", None, None)

    filenames = {name for name, _content in items}
    assert any(visible.database_name in name for name in filenames)
    assert any(hidden.database_name in name for name in filenames)


async def test_export_type_empty_when_nothing_accessible(two_databases) -> None:
    from superset.importexport.manager import AsyncFullAssetManager

    session, _visible, _hidden = two_databases
    manager = AsyncFullAssetManager(session)
    sm = AsyncMock()
    user = SimpleNamespace(id=1)

    async def _deny_all(_sm, _user):
        return [sa.text("0=1")]

    with patch(
        "superset.db.filters.database_access_filters",
        new=_deny_all,
    ):
        items = await manager._export_type("databases", sm, user)

    assert items == []
