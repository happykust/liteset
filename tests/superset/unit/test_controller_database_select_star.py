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
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import (
    NoSuchTableError,
)

from superset.controllers.database import (
    _engine_select_star_sync,
    DatabaseController,
)
from superset.exceptions import ObjectNotFoundError
from superset.sql.parse import Table


def _get_raw_method(method_name: str):
    handler = getattr(DatabaseController, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


_select_star = _get_raw_method("select_star")
_select_star_with_schema = _get_raw_method("select_star_with_schema")


def _make_database_mock(*, raise_no_such_table: bool = False) -> MagicMock:
    db_engine_spec = MagicMock()
    if raise_no_such_table:
        db_engine_spec.select_star.side_effect = NoSuchTableError("no_such")
    else:
        db_engine_spec.select_star.return_value = 'SELECT *\nFROM "my_table"\nLIMIT 100'

    engine_cm = MagicMock()
    engine_cm.__enter__ = MagicMock(return_value=MagicMock())
    engine_cm.__exit__ = MagicMock(return_value=False)

    database = MagicMock()
    database.db_engine_spec = db_engine_spec
    database.get_sqla_engine.return_value = engine_cm
    database.get_default_catalog.return_value = None
    database.sqlalchemy_uri = "postgresql://localhost/db"
    return database


def test_engine_select_star_sync_reraises_no_such_table() -> None:
    """``_engine_select_star_sync`` must NOT swallow ``NoSuchTableError`` in the
    broad ``except Exception`` fallback — the error must propagate to the endpoint."""
    database = _make_database_mock(raise_no_such_table=True)
    table = Table("nonexistent_table", None)

    with pytest.raises(NoSuchTableError):
        _engine_select_star_sync(database, table)


def test_engine_select_star_sync_returns_sql_on_success() -> None:
    database = _make_database_mock(raise_no_such_table=False)
    table = Table("my_table", None)

    sql = _engine_select_star_sync(database, table)
    assert "my_table" in sql


def test_engine_select_star_sync_fallback_on_generic_error() -> None:
    db_engine_spec = MagicMock()
    db_engine_spec.select_star.side_effect = RuntimeError("generic engine error")
    del db_engine_spec.quote_table  # ensure the quote_table branch is skipped

    engine_cm = MagicMock()
    engine_cm.__enter__ = MagicMock(return_value=MagicMock())
    engine_cm.__exit__ = MagicMock(return_value=False)

    database = MagicMock()
    database.db_engine_spec = db_engine_spec
    database.get_sqla_engine.return_value = engine_cm
    database.get_default_catalog.return_value = None
    database.sqlalchemy_uri = "postgresql://localhost/db"

    table = Table("my_table", "public")
    sql = _engine_select_star_sync(database, table)
    assert "my_table" in sql
    assert "public" in sql


@pytest.mark.asyncio
async def test_select_star_no_such_table_raises_404() -> None:
    """GET /{pk}/select_star/{table_name}/ on a non-existent table → 404."""
    dao = AsyncMock()
    dao.find_by_id.return_value = MagicMock()

    sm = MagicMock()
    sm.raise_for_access = AsyncMock(return_value=None)

    with patch(
        "superset.controllers.database._engine_select_star",
        new=AsyncMock(side_effect=NoSuchTableError("nonexistent")),
    ):
        with pytest.raises(ObjectNotFoundError) as exc_info:
            await _select_star(
                self=None,
                pk=1,
                table_name="nonexistent",
                dao=dao,
                security_manager=sm,
                current_user=MagicMock(),
                schema_name="",
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_select_star_success_returns_sql() -> None:
    dao = AsyncMock()
    dao.find_by_id.return_value = MagicMock()

    sm = MagicMock()
    sm.raise_for_access = AsyncMock(return_value=None)

    expected_sql = 'SELECT *\nFROM "my_table"\nLIMIT 100'

    with patch(
        "superset.controllers.database._engine_select_star",
        new=AsyncMock(return_value=expected_sql),
    ):
        result = await _select_star(
            self=None,
            pk=1,
            table_name="my_table",
            dao=dao,
            security_manager=sm,
            current_user=MagicMock(),
            schema_name="",
        )

    assert result.result == expected_sql


@pytest.mark.asyncio
async def test_select_star_with_schema_no_such_table_raises_404() -> None:
    """GET /{pk}/select_star/{table}/{schema}/ on a non-existent table → 404."""
    dao = AsyncMock()
    dao.find_by_id.return_value = MagicMock()

    sm = MagicMock()
    sm.raise_for_access = AsyncMock(return_value=None)

    with patch(
        "superset.controllers.database._engine_select_star",
        new=AsyncMock(side_effect=NoSuchTableError("nonexistent")),
    ):
        with pytest.raises(ObjectNotFoundError) as exc_info:
            await _select_star_with_schema(
                self=None,
                pk=1,
                table_name="nonexistent",
                schema_name="public",
                dao=dao,
                security_manager=sm,
                current_user=MagicMock(),
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_select_star_with_schema_success_returns_sql() -> None:
    dao = AsyncMock()
    dao.find_by_id.return_value = MagicMock()

    sm = MagicMock()
    sm.raise_for_access = AsyncMock(return_value=None)

    expected_sql = 'SELECT *\nFROM "public"."my_table"\nLIMIT 100'

    with patch(
        "superset.controllers.database._engine_select_star",
        new=AsyncMock(return_value=expected_sql),
    ):
        result = await _select_star_with_schema(
            self=None,
            pk=1,
            table_name="my_table",
            schema_name="public",
            dao=dao,
            security_manager=sm,
            current_user=MagicMock(),
        )

    assert result.result == expected_sql
