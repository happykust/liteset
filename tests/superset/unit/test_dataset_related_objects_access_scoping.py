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
"""Regression: ``GET /api/v1/dataset/{id_or_uuid}/related_objects`` must be
access-scoped, exactly like the sibling ``GET /api/v1/dataset/{pk}``.

``DatasetController.related_objects`` used to fetch the dataset via
``dao.find_by_id(pk)`` (int branch) / an unfiltered ``SqlaTable.uuid``
lookup (UUID branch) with no access filter at all, so a stock Gamma could
walk the id range and harvest chart names/viz types plus dashboard ids/
slugs/titles/``json_metadata`` for datasets they cannot otherwise see.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from superset.exceptions import ObjectNotFoundError


def _raw_method(controller_cls: type, method_name: str):
    """Unwrap a Litestar route handler back to its plain coroutine function."""
    handler = getattr(controller_cls, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


@pytest.fixture
async def dataset_env():
    """In-memory SQLite session with one Database + one SqlaTable row."""
    import superset.models  # noqa: F401  (register models)
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
        db = Database(database_name="examples", sqlalchemy_uri="sqlite://")
        session.add(db)
        await session.flush()
        dataset = SqlaTable(table_name="secret_table", database_id=db.id)
        session.add(dataset)
        await session.commit()
        yield session, dataset
    await engine.dispose()


async def test_related_objects_404s_when_dataset_not_accessible(dataset_env) -> None:
    from superset.controllers.dataset import DatasetController
    from superset.db.daos.dataset import AsyncDatasetDAO

    session, dataset = dataset_env
    dao = AsyncDatasetDAO(session)
    controller = DatasetController(owner=MagicMock())
    related_objects = _raw_method(DatasetController, "related_objects")

    with patch(
        "superset.db.filters.dataset_access_filters",
        new=AsyncMock(return_value=[sa.text("0=1")]),  # deny everything
    ):
        with pytest.raises(ObjectNotFoundError):
            await related_objects(
                controller,
                id_or_uuid=str(dataset.id),
                dao=dao,
                security_manager=AsyncMock(),
                current_user=SimpleNamespace(id=1),
            )


async def test_related_objects_succeeds_when_dataset_accessible(dataset_env) -> None:
    from superset.controllers.dataset import DatasetController
    from superset.db.daos.dataset import AsyncDatasetDAO

    session, dataset = dataset_env
    dao = AsyncDatasetDAO(session)
    controller = DatasetController(owner=MagicMock())
    related_objects = _raw_method(DatasetController, "related_objects")

    with patch(
        "superset.db.filters.dataset_access_filters",
        new=AsyncMock(return_value=[]),  # admin / full access -> no restriction
    ):
        result = await related_objects(
            controller,
            id_or_uuid=str(dataset.id),
            dao=dao,
            security_manager=AsyncMock(),
            current_user=SimpleNamespace(id=1),
        )

    assert result["charts"]["count"] == 0
    assert result["dashboards"]["count"] == 0
