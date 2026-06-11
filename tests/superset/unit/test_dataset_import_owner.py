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
"""Regression: the importing user becomes the owner of an imported dataset.

The owner block was gated on ``hasattr(security_manager, "get_current_user")``
which is ALWAYS False on AsyncSecurityManager → the importer was never set as
owner. The fix resolves the user from the request-scoped ContextVar (the async
equivalent of upstream's ``g.user`` / ``get_user()``).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload


@pytest.fixture
async def import_env():
    import superset.models  # noqa: F401  (register models)
    from superset.models.connectors import SqlaTable  # noqa: F401
    from superset.models.core import Database
    from superset.models.helpers import Base
    from superset.models.security import User

    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    # Reuse the same in-memory DB across sync seed + async import.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        db = Database(
            database_name="examples",
            sqlalchemy_uri="sqlite://",
            uuid="aaaaaaaa-0000-0000-0000-000000000001",
        )
        user = User(
            username="importer",
            first_name="im",
            last_name="porter",
            email="importer@test.com",
            active=True,
        )
        session.add_all([db, user])
        await session.commit()
        yield session, db, user
    await engine.dispose()


async def test_importing_user_becomes_dataset_owner(import_env):
    from superset.commands.dataset.importers.v1 import ImportDatasetsCommand
    from superset.db.daos.dataset import AsyncDatasetDAO
    from superset.models.connectors import SqlaTable
    from superset.utils.core import set_current_user

    session, db, user = import_env
    set_current_user(user)

    cmd = ImportDatasetsCommand.__new__(ImportDatasetsCommand)
    cmd._dao = AsyncDatasetDAO(session)
    cmd._security_manager = None
    cmd._ignore_permissions = True
    cmd._sync_columns = False
    cmd._sync_metrics = False
    cmd._force_data = False

    await cmd._import_dataset(
        {
            "uuid": "bbbbbbbb-0000-0000-0000-000000000001",
            "table_name": "energy",
            "database_uuid": "aaaaaaaa-0000-0000-0000-000000000001",
            "database_id": db.id,
            "columns": [],
            "metrics": [],
        }
    )
    await session.commit()

    dataset = (
        (
            await session.execute(
                select(SqlaTable)
                .options(selectinload(SqlaTable.owners))
                .where(SqlaTable.table_name == "energy")
            )
        )
        .scalars()
        .first()
    )
    assert dataset is not None
    assert [o.username for o in dataset.owners] == ["importer"]


async def test_import_name_collision_keeps_uuid_row_unmodified(import_env):
    """Historical two-row collision: keep the UUID-matched row unmodified.

    Upstream caught ``MultipleResultsFound`` from the OR-dedup lookup and
    returned the UUID-matched row as-is. The async port's overwrite-update
    used to push the colliding (schema, table_name) into ``flush()`` →
    IntegrityError on ``uq_tables_database_catalog_schema_table`` → 500.
    """
    from superset.commands.dataset.importers.v1 import ImportDatasetsCommand
    from superset.db.daos.dataset import AsyncDatasetDAO
    from superset.models.connectors import SqlaTable
    from superset.utils.core import set_current_user

    session, db, user = import_env
    set_current_user(user)

    # Row A: the legacy schema-less import (UUID-matched on re-import).
    row_a = SqlaTable(
        table_name="users",
        schema=None,
        database_id=db.id,
        uuid="cccccccc-0000-0000-0000-00000000000a",
        owners=[user],
    )
    # Row B: a different-UUID dataset already occupying the incoming name.
    row_b = SqlaTable(
        table_name="users",
        schema="public",
        database_id=db.id,
        uuid="cccccccc-0000-0000-0000-00000000000b",
        owners=[],
    )
    session.add_all([row_a, row_b])
    await session.commit()

    cmd = ImportDatasetsCommand.__new__(ImportDatasetsCommand)
    cmd._dao = AsyncDatasetDAO(session)
    cmd._security_manager = None
    cmd._ignore_permissions = True
    cmd._sync_columns = False
    cmd._sync_metrics = False
    cmd._force_data = False
    cmd._overwrite = True

    # Before the fix: IntegrityError out of session.flush(). Now: no-op skip.
    await cmd._import_dataset(
        {
            "uuid": "cccccccc-0000-0000-0000-00000000000a",
            "table_name": "users",
            "schema": "public",
            "database_uuid": "aaaaaaaa-0000-0000-0000-000000000001",
            "database_id": db.id,
            "columns": [],
            "metrics": [],
        }
    )
    await session.commit()

    refreshed = (
        (
            await session.execute(
                select(SqlaTable).where(
                    SqlaTable.uuid == "cccccccc-0000-0000-0000-00000000000a"
                )
            )
        )
        .scalars()
        .one()
    )
    # UUID-matched row stays unmodified (schema NOT updated to "public").
    assert refreshed.schema is None
    assert refreshed.table_name == "users"
