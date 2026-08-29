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
"""Regression: chart/dataset bundle import must not create or overwrite a
database connection without a real ``can_write Database`` check.

``_import_database``/``_import_dataset`` (superset/commands/chart/importers/
v1/utils.py) used to default ``ignore_permissions=True``, making
``can_write = ignore_permissions`` unconditionally ``True`` and the
``elif not can_write: raise ImportFailedError(...)`` branch unreachable. Any
authenticated user able to reach a chart/dashboard/dataset import endpoint
(gated only on ``can_write Chart``/``can_write Dataset``, NOT
``can_write Database``) could therefore create a brand-new ``databases/*``
entry with an attacker-controlled ``sqlalchemy_uri``, regardless of whether
they held the ``can_write Database`` permission.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from superset.exceptions import ImportFailedError


@pytest.fixture
async def import_session():
    """In-memory SQLite session with the real model metadata registered."""
    import superset.models  # noqa: F401  (register models)
    from superset.models.helpers import Base

    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _db_config(uuid: str) -> dict[str, object]:
    # ``postgresql://`` (rather than ``sqlite://``) so the URI clears the
    # unrelated ``PREVENT_UNSAFE_DB_CONNECTIONS`` blocklist check in
    # ``_import_database`` — this test is only about the ``can_write``
    # permission gate, not the sqlite/shillelagh filesystem-escape guard.
    # ``127.0.0.1:1`` (a reserved, almost-certainly-closed local port)
    # instead of a real/fake remote host so the best-effort
    # ``add_permissions`` connection attempt fails instantly (connection
    # refused) rather than risking a slow DNS/TCP timeout in CI.
    return {
        "database_name": "attacker_db",
        "sqlalchemy_uri": "postgresql://user:pass@127.0.0.1:1/attackerdb",
        "uuid": uuid,
        "extra": {},
    }


async def test_import_database_refused_without_can_write(import_session) -> None:
    """A user lacking ``can_write Database`` cannot create one via import."""
    from superset.commands.chart.importers.v1.utils import _import_database

    denied_sm = AsyncMock()
    denied_sm.can_access = AsyncMock(return_value=False)

    from superset.utils.core import set_current_user

    set_current_user(object())  # any non-None "current user"

    with pytest.raises(ImportFailedError):
        await _import_database(
            import_session,
            _db_config("11111111-0000-0000-0000-000000000001"),
            security_manager=denied_sm,
        )
    denied_sm.can_access.assert_awaited_once()
    call = denied_sm.can_access.await_args
    assert call.args == ("can_write", "Database")
    assert call.kwargs["user"] is not None


async def test_import_database_allowed_with_can_write(import_session) -> None:
    """The same import succeeds once ``can_write Database`` is granted."""
    from sqlalchemy import select

    from superset.commands.chart.importers.v1.utils import _import_database
    from superset.models.core import Database
    from superset.utils.core import set_current_user

    allowed_sm = AsyncMock()
    allowed_sm.can_access = AsyncMock(return_value=True)
    set_current_user(object())

    database = await _import_database(
        import_session,
        _db_config("11111111-0000-0000-0000-000000000002"),
        security_manager=allowed_sm,
    )
    await import_session.commit()

    assert database.id is not None
    stored = (
        (
            await import_session.execute(
                select(Database).where(
                    Database.uuid == "11111111-0000-0000-0000-000000000002"
                )
            )
        )
        .scalars()
        .one()
    )
    assert stored.database_name == "attacker_db"


async def test_import_dataset_refused_without_can_write(import_session) -> None:
    """The same ``ignore_permissions`` bug, ported to ``_import_dataset``."""
    from superset.commands.chart.importers.v1.utils import _import_dataset
    from superset.models.core import Database
    from superset.utils.core import set_current_user

    db = Database(database_name="examples", sqlalchemy_uri="sqlite://")
    import_session.add(db)
    await import_session.commit()

    denied_sm = AsyncMock()
    denied_sm.can_access = AsyncMock(return_value=False)
    user = object()
    set_current_user(user)

    with pytest.raises(ImportFailedError):
        await _import_dataset(
            import_session,
            {
                "uuid": "22222222-0000-0000-0000-000000000001",
                "table_name": "attacker_table",
                "database_id": db.id,
                "columns": [],
                "metrics": [],
            },
            security_manager=denied_sm,
            current_user=user,
        )
    denied_sm.can_access.assert_awaited_once_with("can_write", "Dataset", user=user)


async def test_import_dataset_allowed_with_can_write(import_session) -> None:
    """The same import succeeds once ``can_write Dataset`` is granted --
    the symmetric counterpart to ``test_import_database_allowed_with_can_write``,
    also covering the owner-assignment path that only runs when a real
    ``current_user`` is threaded through.
    """
    from sqlalchemy import select

    from superset.commands.chart.importers.v1.utils import _import_dataset
    from superset.models.connectors import SqlaTable
    from superset.models.core import Database
    from superset.models.security import User
    from superset.utils.core import set_current_user

    db = Database(database_name="examples", sqlalchemy_uri="sqlite://")
    import_session.add(db)
    user = User(
        first_name="Import",
        last_name="Er",
        username="importer",
        email="importer@test.com",
    )
    import_session.add(user)
    await import_session.commit()

    allowed_sm = AsyncMock()
    allowed_sm.can_access = AsyncMock(return_value=True)
    set_current_user(user)

    dataset = await _import_dataset(
        import_session,
        {
            "uuid": "22222222-0000-0000-0000-000000000002",
            "table_name": "attacker_table",
            "database_id": db.id,
            "columns": [],
            "metrics": [],
        },
        security_manager=allowed_sm,
        current_user=user,
    )
    await import_session.commit()

    allowed_sm.can_access.assert_awaited_once_with("can_write", "Dataset", user=user)
    assert dataset.id is not None
    stored = (
        (
            await import_session.execute(
                select(SqlaTable).where(
                    SqlaTable.uuid == "22222222-0000-0000-0000-000000000002"
                )
            )
        )
        .scalars()
        .one()
    )
    assert stored.table_name == "attacker_table"
    assert user in stored.owners
