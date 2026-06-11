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
"""R13-08: ``get_datasets`` must filter catalog/schema UNCONDITIONALLY when
the argument is supplied (``None`` → ``IS NULL``), 1:1 with upstream
``DatabaseDAO.get_datasets``. The previous conditional semantics made
``SyncPermissionsCommand`` rewrite perms on datasets of ALL catalogs when
``catalog=None``. Omitting the argument keeps the all-datasets contract that
the export flow relies on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def dao_env():
    import superset.models  # noqa: F401  (register models)
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.models.connectors import SqlaTable
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
        db = Database(database_name="db", sqlalchemy_uri="sqlite://")
        session.add(db)
        await session.flush()
        for catalog, schema, name in [
            (None, "public", "t_null_catalog"),
            ("stale", "public", "t_stale_catalog"),
            (None, "other", "t_other_schema"),
        ]:
            session.add(
                SqlaTable(
                    table_name=name,
                    database_id=db.id,
                    catalog=catalog,
                    schema=schema,
                )
            )
        await session.commit()
        yield AsyncDatabaseDAO(session), db.id
    await engine.dispose()


async def test_explicit_none_catalog_filters_is_null(dao_env):
    dao, db_id = dao_env
    datasets = await dao.get_datasets(db_id, catalog=None, schema="public")
    # Upstream scopes to catalog IS NULL — the stale-catalog dataset must
    # NOT be returned (the conditional port returned both).
    assert sorted(t.table_name for t in datasets) == ["t_null_catalog"]


async def test_explicit_catalog_value_filters(dao_env):
    dao, db_id = dao_env
    datasets = await dao.get_datasets(db_id, catalog="stale", schema="public")
    assert sorted(t.table_name for t in datasets) == ["t_stale_catalog"]


async def test_omitted_arguments_return_all(dao_env):
    dao, db_id = dao_env
    datasets = await dao.get_datasets(db_id)
    assert len(datasets) == 3
