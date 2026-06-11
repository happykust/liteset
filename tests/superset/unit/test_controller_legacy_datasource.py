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
"""Unit tests for ``superset/controllers/legacy_datasource.py``.

Regression guard for the finding:

    Non-existent physical table returns HTTP 200 with empty list instead of
    HTTP 404 when ``_inspect_sync`` swallows ``NoSuchTableError``.

Original behaviour documented in
``superset_old/connectors/sqla/utils.py:59-61``::

    if not (database.has_table(table) or database.has_view(table)):
        raise NoSuchTableError(table)

and ``superset_old/views/datasource/views.py:190-191``::

    except (NoResultFound, NoSuchTableError) as ex:
        raise DatasetNotFoundError() from ex  # → HTTP 404

The fix ensures ``_get_physical_table_metadata_async`` propagates
``NoSuchTableError`` so ``external_metadata_by_name`` returns 404.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import NoSuchTableError

from superset.controllers.legacy_datasource import (
    _get_physical_table_metadata_async,
    LegacyDatasourceController,
)
from superset.sql.parse import Table

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_raw_method(method_name: str) -> Any:
    handler = getattr(LegacyDatasourceController, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


_external_metadata_by_name = _get_raw_method("external_metadata_by_name")


def _make_mock_inspector(
    *,
    table_exists: bool = True,
    views: list[str] | None = None,
    columns: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Build a minimal SQLAlchemy Inspector mock."""
    inspector = MagicMock()
    inspector.has_table.return_value = table_exists
    inspector.get_view_names.return_value = views or []
    inspector.get_columns.return_value = columns or [
        {"name": "id", "type": MagicMock(__str__=lambda s: "INTEGER"), "comment": None}
    ]
    return inspector


def _make_async_conn(inspector: MagicMock) -> AsyncMock:
    """Build an async connection whose run_sync calls the sync function."""

    async def _run_sync(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(_connection_inner)

    _connection_inner = MagicMock()
    conn = AsyncMock()
    conn.run_sync = _run_sync
    return conn


@contextlib.asynccontextmanager
async def _async_conn_cm(inspector: MagicMock):  # type: ignore[misc]
    """Async context manager that yields (mock_conn, mock_spec)."""

    async def _run_sync(fn: Any, *args: Any, **kwargs: Any) -> Any:
        with patch("sqlalchemy.inspect", return_value=inspector):
            return fn(MagicMock())

    conn = AsyncMock()
    conn.run_sync = _run_sync
    yield conn, MagicMock()


# ---------------------------------------------------------------------------
# _get_physical_table_metadata_async — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_physical_metadata_raises_no_such_table_for_missing_table() -> None:
    """Non-existent table (not in tables OR views) raises NoSuchTableError.

    Regression: before the fix, NoSuchTableError was swallowed by
    ``except Exception: return []``, yielding 200 with an empty list.
    """
    inspector = _make_mock_inspector(table_exists=False, views=[])
    database = MagicMock()
    table = Table("nonexistent_table", None)

    # get_async_connection is a local import inside _get_physical_table_metadata_async;
    # patch at the source module so the function sees the patched version.
    with patch(
        "superset.utils.database.get_async_connection",
        new=lambda db: _async_conn_cm(inspector),
    ):
        with pytest.raises(NoSuchTableError):
            await _get_physical_table_metadata_async(database, table)


@pytest.mark.asyncio
async def test_physical_metadata_view_exists_returns_columns() -> None:
    """Table not in tables but listed in views — returns column metadata, no raise."""
    inspector = _make_mock_inspector(
        table_exists=False,
        views=["nonexistent_table"],
        columns=[
            {
                "name": "col1",
                "type": MagicMock(__str__=lambda s: "TEXT"),
                "comment": None,
            }
        ],
    )
    database = MagicMock()
    table = Table("nonexistent_table", None)

    with patch(
        "superset.utils.database.get_async_connection",
        new=lambda db: _async_conn_cm(inspector),
    ):
        result = await _get_physical_table_metadata_async(database, table)
    # View exists → should return column metadata, not raise
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "col1"


@pytest.mark.asyncio
async def test_physical_metadata_returns_columns_for_existing_table() -> None:
    """Existing table returns column metadata without raising."""
    inspector = _make_mock_inspector(
        table_exists=True,
        columns=[
            {
                "name": "id",
                "type": MagicMock(__str__=lambda s: "INTEGER"),
                "comment": None,
            },
            {
                "name": "name",
                "type": MagicMock(__str__=lambda s: "VARCHAR(255)"),
                "comment": "user name",
            },
        ],
    )
    database = MagicMock()
    table = Table("my_table", "public")

    with patch(
        "superset.utils.database.get_async_connection",
        new=lambda db: _async_conn_cm(inspector),
    ):
        result = await _get_physical_table_metadata_async(database, table)

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["name"] == "id"
    assert result[0]["type"] == "INTEGER"
    assert result[1]["name"] == "name"
    # Type string with "(" is truncated to prefix
    assert result[1]["type"] == "VARCHAR"
    assert result[1]["longType"] == "VARCHAR(255)"
    assert result[1]["comment"] == "user name"


@pytest.mark.asyncio
async def test_physical_metadata_normalize_columns() -> None:
    """normalize_columns=True lowercases the column name."""
    inspector = _make_mock_inspector(
        table_exists=True,
        columns=[
            {
                "name": "UserId",
                "type": MagicMock(__str__=lambda s: "INTEGER"),
                "comment": None,
            }
        ],
    )
    database = MagicMock()
    table = Table("tbl", None)

    with patch(
        "superset.utils.database.get_async_connection",
        new=lambda db: _async_conn_cm(inspector),
    ):
        result = await _get_physical_table_metadata_async(
            database, table, normalize_columns=True
        )

    assert result[0]["name"] == "userid"


# ---------------------------------------------------------------------------
# external_metadata_by_name — integration of the 404 path
# ---------------------------------------------------------------------------


def _make_ds_dao_mock(database_obj: Any) -> AsyncMock:
    """Build a minimal ds_dao mock for external_metadata_by_name."""
    dao = AsyncMock()
    # session.execute returns scalars().first() == database_obj
    exec_result = MagicMock()
    exec_result.scalars.return_value.first.return_value = database_obj
    dao.session.execute = AsyncMock(return_value=exec_result)
    # _get_datasource_by_name internal query (second session.execute call)
    return dao


@pytest.mark.asyncio
async def test_external_metadata_by_name_404_for_nonexistent_table() -> None:
    """GET external_metadata_by_name returns 404 when the physical table doesn't exist.

    Regression: before fix, _get_physical_table_metadata_async swallowed
    NoSuchTableError and the endpoint returned 200 with [].
    """
    import prison

    database_mock = MagicMock()

    # First session.execute (for _get_datasource_by_name) returns no rows
    ds_result = MagicMock()
    ds_result.scalars.return_value.all.return_value = []

    # Second session.execute (for Database lookup) returns the database mock
    db_result = MagicMock()
    db_result.scalars.return_value.first.return_value = database_mock

    ds_dao = AsyncMock()
    ds_dao.session.execute = AsyncMock(side_effect=[ds_result, db_result])

    request = MagicMock()
    q_val = prison.dumps(
        {
            "datasource_type": "table",
            "database_name": "my_db",
            "table_name": "missing_table",
            "schema_name": "public",
        }
    )
    request.query_params = {"q": q_val}

    with patch(
        "superset.controllers.legacy_datasource._get_physical_table_metadata_async",
        side_effect=NoSuchTableError("missing_table"),
    ):
        response = await _external_metadata_by_name(
            None, request=request, ds_dao=ds_dao
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_external_metadata_by_name_200_for_existing_table() -> None:
    """GET external_metadata_by_name returns 200 with column list for existing table."""
    import prison

    database_mock = MagicMock()

    ds_result = MagicMock()
    ds_result.scalars.return_value.all.return_value = []

    db_result = MagicMock()
    db_result.scalars.return_value.first.return_value = database_mock

    ds_dao = AsyncMock()
    ds_dao.session.execute = AsyncMock(side_effect=[ds_result, db_result])

    request = MagicMock()
    q_val = prison.dumps(
        {
            "datasource_type": "table",
            "database_name": "my_db",
            "table_name": "real_table",
            "schema_name": "public",
        }
    )
    request.query_params = {"q": q_val}

    cols = [{"name": "id", "type": "INTEGER", "longType": "INTEGER", "comment": None}]

    with patch(
        "superset.controllers.legacy_datasource._get_physical_table_metadata_async",
        new=AsyncMock(return_value=cols),
    ):
        response = await _external_metadata_by_name(
            None, request=request, ds_dao=ds_dao
        )

    assert response.status_code == 200
    assert response.content == cols


# ---------------------------------------------------------------------------
# save() — database_id unconditional assignment (regression for null edge case)
# ---------------------------------------------------------------------------
# Original: superset_old/views/datasource/views.py:87+91
#   database_id = datasource_dict["database"].get("id")
#   orm_datasource.database_id = database_id   ← NO None guard
#
# Regression: liteset guarded with ``if database_id is not None:`` which
# silently skipped the assignment for ``{"database": {"id": null}}``.  Fix
# removes the guard so the original unconditional write is preserved.
# ---------------------------------------------------------------------------

_save = LegacyDatasourceController.save.fn  # type: ignore[attr-defined]


def _make_save_request(payload: dict[str, Any]) -> MagicMock:
    """Build a minimal Litestar Request mock for the save endpoint."""
    request = MagicMock()
    form_data = MagicMock()
    form_data.get = lambda key, default=None: (
        json.dumps(payload) if key == "data" else default
    )
    request.form = AsyncMock(return_value=form_data)
    request.app = MagicMock()
    return request


def _make_ds_dao_for_save(orm_datasource: Any) -> AsyncMock:
    """Build a minimal ds_dao mock for the save endpoint."""
    ds_dao = AsyncMock()
    ds_dao.get_datasource = AsyncMock(return_value=orm_datasource)
    ds_dao.session = AsyncMock()
    ds_dao.session.commit = AsyncMock()
    ds_dao.session.rollback = AsyncMock()
    return ds_dao


@pytest.mark.asyncio
async def test_save_assigns_database_id_none_when_explicitly_null() -> None:
    """save() must set orm_datasource.database_id = None for ``database.id: null``.

    Regression: the ``if database_id is not None:`` guard in liteset skipped
    the assignment entirely.  Original (views/datasource/views.py:91) always
    writes ``orm_datasource.database_id = database_id`` unconditionally.
    """
    orm_datasource = MagicMock()
    orm_datasource.data = {"id": 1, "columns": []}
    orm_datasource.owner_class = None  # skip the ownership check branch

    payload = {
        "id": 1,
        "type": "table",
        "database": {"id": None},  # explicit null — triggers the regression
        "columns": [],
        # ``owners`` is REQUIRED: the original accesses
        # ``datasource_dict["owners"]`` unconditionally (views.py:100-102) —
        # an owners-less payload is a KeyError → 500, not a 200 save.
        "owners": [],
    }
    request = _make_save_request(payload)
    ds_dao = _make_ds_dao_for_save(orm_datasource)
    current_user = MagicMock()

    # Prevent actual security-manager construction and to_thread execution.
    mock_sec_mgr = MagicMock()
    mock_sec_mgr.raise_for_access = AsyncMock(return_value=None)
    mock_sec_mgr.find_user_by_id = AsyncMock(return_value=MagicMock(id=1))
    mock_sec_mgr.is_admin = MagicMock(return_value=True)
    with (
        patch(
            "superset.dependencies.provide_security_manager",
            new=AsyncMock(return_value=mock_sec_mgr),
        ),
        patch("asyncio.to_thread", new=AsyncMock(return_value=None)),
        patch(
            "superset.controllers.legacy_datasource._sanitize_datasource_data",
            return_value={"id": 1},
        ),
    ):
        response = await _save(
            None,
            request=request,
            ds_dao=ds_dao,
            current_user=current_user,
        )

    # Must succeed (200) — the None write should not cause an early return.
    assert response.status_code == 200
    # The key assertion: database_id was set to None, not silently skipped.
    assert orm_datasource.database_id is None


@pytest.mark.asyncio
async def test_save_assigns_database_id_integer_value() -> None:
    """save() sets orm_datasource.database_id to the supplied integer id."""
    orm_datasource = MagicMock()
    orm_datasource.data = {"id": 1, "columns": []}
    orm_datasource.owner_class = None  # skip the ownership check branch

    payload = {
        "id": 1,
        "type": "table",
        "database": {"id": 42},
        "columns": [],
        "owners": [],  # required — see test above
    }
    request = _make_save_request(payload)
    ds_dao = _make_ds_dao_for_save(orm_datasource)
    current_user = MagicMock()

    mock_sec_mgr = MagicMock()
    mock_sec_mgr.raise_for_access = AsyncMock(return_value=None)
    mock_sec_mgr.find_user_by_id = AsyncMock(return_value=MagicMock(id=1))
    mock_sec_mgr.is_admin = MagicMock(return_value=True)
    with (
        patch(
            "superset.dependencies.provide_security_manager",
            new=AsyncMock(return_value=mock_sec_mgr),
        ),
        patch("asyncio.to_thread", new=AsyncMock(return_value=None)),
        patch(
            "superset.controllers.legacy_datasource._sanitize_datasource_data",
            return_value={"id": 1},
        ),
    ):
        response = await _save(
            None,
            request=request,
            ds_dao=ds_dao,
            current_user=current_user,
        )

    assert response.status_code == 200
    assert orm_datasource.database_id == 42


# ---------------------------------------------------------------------------
# samples — failure statuses must be 422 (DatasetSamplesFailedError semantics)
# ---------------------------------------------------------------------------

_samples = _get_raw_method("samples")


def _samples_params() -> dict[str, Any]:
    return {
        "datasource_type": "table",
        "datasource_id": 1,
        "force": False,
        "page": 1,
        "per_page": 10,
        "dashboard_id": None,
    }


def _samples_mocks() -> tuple[Any, Any, Any]:
    """(request, ds_dao, current_user) for the samples handler."""
    request = MagicMock()
    datasource = MagicMock()
    datasource.type = "table"
    datasource.id = 1
    datasource.columns = []
    ds_dao = MagicMock()
    ds_dao.session = MagicMock()
    ds_dao.get_datasource = AsyncMock(return_value=datasource)
    return request, ds_dao, MagicMock()


async def _run_samples_with(count_payload: Any, sample_payload: Any) -> Any:
    """Drive the samples handler with stubbed query-context processors."""
    import types

    request, ds_dao, current_user = _samples_mocks()

    sec_mgr = MagicMock()
    sec_mgr.is_guest_user.return_value = False
    sec_mgr.raise_for_access = AsyncMock(return_value=None)

    samples_proc = MagicMock()
    samples_proc.get_payload = AsyncMock(return_value=sample_payload)
    count_proc = MagicMock()
    count_proc.get_payload = AsyncMock(return_value=count_payload)

    def _qo_factory(**kwargs: Any) -> Any:
        return types.SimpleNamespace(**kwargs)

    with (
        patch(
            "superset.controllers.legacy_datasource._parse_samples_params",
            return_value=(_samples_params(), None, []),
        ),
        patch(
            "superset.controllers.legacy_datasource._parse_samples_payload",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "superset.dependencies.provide_security_manager",
            new=AsyncMock(return_value=sec_mgr),
        ),
        patch(
            "superset.controllers.legacy_datasource.AsyncQueryObject",
            side_effect=_qo_factory,
        ),
        patch("superset.controllers.legacy_datasource.AsyncQueryContext", MagicMock()),
        patch(
            "superset.controllers.legacy_datasource.AsyncQueryContextProcessor",
            side_effect=[samples_proc, count_proc],
        ),
    ):
        return await _samples(
            None, request=request, ds_dao=ds_dao, current_user=current_user
        )


@pytest.mark.asyncio
async def test_samples_count_query_failed_returns_422() -> None:
    """count(*) status=failed → 422, 1:1 with DatasetSamplesFailedError.

    Original: superset_old/views/datasource/utils.py:158-159 raises
    ``DatasetSamplesFailedError`` (CommandInvalidError → status 422,
    superset_old/commands/exceptions.py:54-57); ``@handle_api_exception``
    returns ``ex.status`` — NOT 400.
    """
    response = await _run_samples_with(
        count_payload={"queries": [{"status": "failed", "error": "boom"}]},
        sample_payload={"queries": [{}]},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_samples_sample_query_failed_returns_422() -> None:
    """sample query status=failed → 422 (utils.py:163-165 → 422)."""
    import pandas as pd

    response = await _run_samples_with(
        count_payload={
            "queries": [
                {
                    "status": "success",
                    "df": pd.DataFrame([{"COUNT(*)": 3}]),
                    "cache_key": None,
                }
            ]
        },
        sample_payload={"queries": [{"status": "failed", "error": "boom"}]},
    )
    assert response.status_code == 422
