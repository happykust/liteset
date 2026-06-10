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
"""Unit tests for DatabaseController.select_star — NoSuchTableError → 404.

Regression guard for the original behaviour documented in
``superset_old/databases/api.py:1236-1238``:

    except NoSuchTableError:
        return self.response(404, message='Table not found on the database')

Prior to the fix, ``_engine_select_star_sync`` swallowed ``NoSuchTableError``
in the broad ``except Exception`` fallback and returned plain SQL, so the
endpoint always returned HTTP 200 even for non-existent tables.
"""

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_raw_method(method_name: str):
    handler = getattr(DatabaseController, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


_select_star = _get_raw_method("select_star")
_select_star_with_schema = _get_raw_method("select_star_with_schema")


def _make_database_mock(*, raise_no_such_table: bool = False) -> MagicMock:
    """Return a mock Database whose engine spec raises NoSuchTableError."""
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


# ---------------------------------------------------------------------------
# _engine_select_star_sync — unit tests
# ---------------------------------------------------------------------------


def test_engine_select_star_sync_reraises_no_such_table() -> None:
    """``_engine_select_star_sync`` must NOT swallow ``NoSuchTableError``.

    Original: ``database.select_star(table, latest_partition=True)`` raises
    ``NoSuchTableError`` which propagates to the endpoint; liteset must not
    catch it in the broad ``except Exception`` fallback.
    """
    database = _make_database_mock(raise_no_such_table=True)
    table = Table("nonexistent_table", None)

    with pytest.raises(NoSuchTableError):
        _engine_select_star_sync(database, table)


def test_engine_select_star_sync_returns_sql_on_success() -> None:
    """Normal path: valid table → SQL string returned."""
    database = _make_database_mock(raise_no_such_table=False)
    table = Table("my_table", None)

    sql = _engine_select_star_sync(database, table)
    assert "my_table" in sql


def test_engine_select_star_sync_fallback_on_generic_error() -> None:
    """Non-NoSuchTableError exceptions still produce fallback SQL (not re-raised)."""
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
    # Should NOT raise — fallback SQL is returned
    sql = _engine_select_star_sync(database, table)
    assert "my_table" in sql
    assert "public" in sql


# ---------------------------------------------------------------------------
# select_star endpoint — NoSuchTableError → ObjectNotFoundError (404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_star_no_such_table_raises_404() -> None:
    """GET /{pk}/select_star/{table_name}/ on a non-existent table → 404.

    Mirrors ``superset_old/databases/api.py:1236-1238``:
        except NoSuchTableError:
            return self.response(404, message='Table not found on the database')
    """
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
    """GET /{pk}/select_star/{table_name}/ on an existing table → 200 with SQL."""
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


# ---------------------------------------------------------------------------
# select_star_with_schema endpoint — NoSuchTableError → ObjectNotFoundError (404)
# ---------------------------------------------------------------------------


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
    """GET /{pk}/select_star/{table}/{schema}/ on an existing table → SQL."""
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
