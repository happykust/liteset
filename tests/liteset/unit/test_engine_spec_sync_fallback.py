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

from liteset.db.engine_specs import get_async_engine_spec
from liteset.db.engine_specs.base import AsyncResultSet
from liteset.db.engine_specs.clickhouse import AsyncClickHouseEngineSpec
from liteset.db.engine_specs.mysql import AsyncMySQLEngineSpec
from liteset.db.engine_specs.postgres import AsyncPostgresEngineSpec
from liteset.db.engine_specs.sync_fallback import (
    SyncFallbackEngineSpec,
    _is_overridden,
    make_async_spec,
)


# --- _is_overridden tests ---


def test_is_overridden_true() -> None:
    class Base:
        def method(self): ...

    class Child(Base):
        def method(self): ...

    assert _is_overridden(Child, "method") is True


def test_is_overridden_false_inherited() -> None:
    class Base:
        def method(self): ...

    class Child(Base):
        pass

    assert _is_overridden(Child, "method") is False


def test_is_overridden_false_missing() -> None:
    class Empty:
        pass

    assert _is_overridden(Empty, "method") is False


# --- make_async_spec tests ---


class FakeSyncSpec:
    engine = "fake_db"
    engine_name = "FakeDB"
    default_driver = "fakedriver"
    _time_grain_expressions = {None: "{col}", "P1D": "DATE({col})"}


def test_make_async_spec_creates_class() -> None:
    spec = make_async_spec(FakeSyncSpec)
    assert spec.engine == "fake_db"
    assert spec.engine_name == "FakeDB (sync fallback)"
    assert spec.default_driver == "fakedriver"
    assert spec._sync_spec is FakeSyncSpec
    assert issubclass(spec, SyncFallbackEngineSpec)


def test_make_async_spec_preserves_time_grains() -> None:
    spec = make_async_spec(FakeSyncSpec)
    grains = spec.get_time_grain_expressions()
    assert grains[None] == "{col}"
    assert grains["P1D"] == "DATE({col})"


async def test_sync_fallback_execute_calls_run_sync() -> None:
    spec = make_async_spec(FakeSyncSpec)

    mock_sync_result = MagicMock()
    mock_sync_result.returns_rows = True
    mock_sync_result.keys.return_value = ["id"]
    mock_sync_result.fetchall.return_value = [(1,), (2,)]
    mock_sync_result.rowcount = 2

    async def fake_run_sync(fn):
        mock_sync_conn = MagicMock()
        mock_sync_conn.execute.return_value = mock_sync_result
        return fn(mock_sync_conn)

    mock_conn = AsyncMock()
    mock_conn.run_sync = fake_run_sync

    rs = await spec.execute(mock_conn, "SELECT id FROM t")
    assert isinstance(rs, AsyncResultSet)
    assert rs.columns == ["id"]
    assert rs.data == [(1,), (2,)]
    assert rs.row_count == 2


async def test_sync_fallback_fetch_data_with_limit() -> None:
    spec = make_async_spec(FakeSyncSpec)

    mock_sync_result = MagicMock()
    mock_sync_result.fetchmany.return_value = [(1,)]

    async def fake_run_sync(fn):
        mock_sync_conn = MagicMock()
        mock_sync_conn.execute.return_value = mock_sync_result
        return fn(mock_sync_conn)

    mock_conn = AsyncMock()
    mock_conn.run_sync = fake_run_sync

    rows = await spec.fetch_data(mock_conn, "SELECT id FROM t", limit=5)
    assert rows == [(1,)]
    mock_sync_result.fetchmany.assert_called_once_with(5)


def test_sync_fallback_extract_errors_delegates() -> None:
    class SyncSpecWithErrors:
        engine = "test"
        engine_name = "Test"

        @classmethod
        def extract_errors(cls, ex):
            return [MagicMock(message="custom error", error_type="CustomError")]

    spec = make_async_spec(SyncSpecWithErrors)
    errors = spec.extract_errors(ValueError("boom"))
    assert errors[0]["message"] == "custom error"
    assert errors[0]["error_type"] == "CustomError"


def test_sync_fallback_extract_errors_fallback() -> None:
    spec = make_async_spec(FakeSyncSpec)
    errors = spec.extract_errors(RuntimeError("oops"))
    assert errors[0]["message"] == "oops"
    assert errors[0]["error_type"] == "RuntimeError"


# --- Registry tests ---


def test_registry_returns_native_postgres() -> None:
    spec = get_async_engine_spec("postgresql")
    assert spec is AsyncPostgresEngineSpec


def test_registry_returns_native_mysql() -> None:
    spec = get_async_engine_spec("mysql")
    assert spec is AsyncMySQLEngineSpec


def test_registry_returns_native_clickhouse() -> None:
    spec = get_async_engine_spec("clickhouse")
    assert spec is AsyncClickHouseEngineSpec


# --- Sync fallback delegation tests ---


class SyncSpecWithGetCatalogNames:
    engine = "test_cat"
    engine_name = "TestCat"

    @staticmethod
    def get_catalog_names(inspector=None):
        return ["catalog_a", "catalog_b"]


class SyncSpecWithGetSchemaNames:
    engine = "test_schema"
    engine_name = "TestSchema"

    @staticmethod
    def get_schema_names(inspector=None, catalog=None):
        return ["schema_a", "schema_b"]


class SyncSpecWithGetTableNames:
    engine = "test_table"
    engine_name = "TestTable"

    @staticmethod
    def get_table_names(inspector=None, schema=None):
        return ["table_a"]


class SyncSpecWithGetColumns:
    engine = "test_cols"
    engine_name = "TestCols"

    @staticmethod
    def get_columns(inspector=None, table_name=None, schema=None):
        return [{"column_name": "id", "data_type": "INTEGER", "is_nullable": False}]


class SyncSpecWithoutOverrides:
    engine = "test_plain"
    engine_name = "TestPlain"


async def test_get_catalog_names_delegates() -> None:
    spec = make_async_spec(SyncSpecWithGetCatalogNames)
    mock_conn = AsyncMock()

    async def fake_run_sync(fn):
        mock_sync_conn = MagicMock()
        mock_inspector = MagicMock()
        # inspect() is called inside _run, we need to patch
        return fn(mock_sync_conn)

    mock_conn.run_sync = fake_run_sync
    # Should call run_sync (delegation path)
    result = await spec.get_catalog_names(mock_conn)
    assert "catalog_a" in result


async def test_get_catalog_names_fallback() -> None:
    spec = make_async_spec(SyncSpecWithoutOverrides)
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("cat1",)]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    result = await spec.get_catalog_names(mock_conn)
    assert "cat1" in result
    mock_conn.execute.assert_called_once()


async def test_get_schema_names_delegates() -> None:
    spec = make_async_spec(SyncSpecWithGetSchemaNames)
    mock_conn = AsyncMock()

    async def fake_run_sync(fn):
        mock_sync_conn = MagicMock()
        return fn(mock_sync_conn)

    mock_conn.run_sync = fake_run_sync
    result = await spec.get_schema_names(mock_conn, catalog="test")
    assert "schema_a" in result


async def test_get_schema_names_fallback() -> None:
    spec = make_async_spec(SyncSpecWithoutOverrides)
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("public",)]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    result = await spec.get_schema_names(mock_conn)
    assert "public" in result


async def test_get_table_names_delegates() -> None:
    spec = make_async_spec(SyncSpecWithGetTableNames)
    mock_conn = AsyncMock()

    async def fake_run_sync(fn):
        mock_sync_conn = MagicMock()
        return fn(mock_sync_conn)

    mock_conn.run_sync = fake_run_sync
    result = await spec.get_table_names(mock_conn, schema="myschema")
    assert "table_a" in result


async def test_get_table_names_fallback() -> None:
    spec = make_async_spec(SyncSpecWithoutOverrides)
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("users",)]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    result = await spec.get_table_names(mock_conn)
    assert "users" in result


async def test_get_columns_delegates() -> None:
    spec = make_async_spec(SyncSpecWithGetColumns)
    mock_conn = AsyncMock()

    async def fake_run_sync(fn):
        mock_sync_conn = MagicMock()
        return fn(mock_sync_conn)

    mock_conn.run_sync = fake_run_sync
    result = await spec.get_columns(mock_conn, table_name="test_t")
    assert len(result) == 1
    assert result[0]["column_name"] == "id"


async def test_get_columns_fallback() -> None:
    spec = make_async_spec(SyncSpecWithoutOverrides)
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("id", "INTEGER", "NO")]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    result = await spec.get_columns(mock_conn, table_name="test_t")
    assert len(result) == 1
    assert result[0]["column_name"] == "id"


async def test_get_columns_type_error_fallback() -> None:
    """If sync spec raises TypeError, fall back to inspector."""

    class SyncSpecBadColumns:
        engine = "test_bad_cols"
        engine_name = "TestBadCols"

        @staticmethod
        def get_columns(**kwargs):
            raise TypeError("unexpected kwarg")

    spec = make_async_spec(SyncSpecBadColumns)
    mock_conn = AsyncMock()

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [
        {"name": "col1", "type": "TEXT", "nullable": True},
    ]

    async def fake_run_sync(fn):
        # In the _run function, inspect() is called on the sync conn
        # We need to mock that
        mock_sync_conn = MagicMock()
        # Patch sqlalchemy.inspect for this context
        with patch("liteset.db.engine_specs.sync_fallback.inspect", return_value=mock_inspector):
            return fn(mock_sync_conn)

    mock_conn.run_sync = fake_run_sync
    result = await spec.get_columns(mock_conn, table_name="test_t")
    assert len(result) == 1
    assert result[0]["column_name"] == "col1"


# --- Registry tests ---


def test_registry_raises_for_unknown_engine() -> None:
    import liteset.db.engine_specs as registry_mod
    from liteset.db.engine_specs import _fallback_cache

    _fallback_cache.pop("totally_unknown_db", None)
    # Reset cached sync spec map so the patched module is used
    registry_mod._sync_spec_map = None

    # Mock the import inside get_async_engine_spec to simulate superset
    # returning no matching spec
    fake_superset_mod = MagicMock()
    fake_superset_mod.load_engine_specs.return_value = []

    with patch.dict("sys.modules", {"superset": MagicMock(), "superset.db_engine_specs": fake_superset_mod}):
        with pytest.raises(ValueError, match="No async engine spec found"):
            get_async_engine_spec("totally_unknown_db")

    registry_mod._sync_spec_map = None


def test_registry_creates_fallback_for_sync_spec() -> None:
    import liteset.db.engine_specs as registry_mod

    class MockSyncSpec:
        engine = "mssql"
        engine_name = "Microsoft SQL Server"
        default_driver = "pymssql"
        _time_grain_expressions = {}

    from liteset.db.engine_specs import _fallback_cache

    _fallback_cache.pop("mssql", None)
    registry_mod._sync_spec_map = None

    fake_superset_mod = MagicMock()
    fake_superset_mod.load_engine_specs.return_value = [MockSyncSpec]

    with patch.dict("sys.modules", {"superset": MagicMock(), "superset.db_engine_specs": fake_superset_mod}):
        spec = get_async_engine_spec("mssql")
        assert spec.engine == "mssql"
        assert issubclass(spec, SyncFallbackEngineSpec)
        # Second call should hit cache
        spec2 = get_async_engine_spec("mssql")
        assert spec2 is spec

    _fallback_cache.pop("mssql", None)
    registry_mod._sync_spec_map = None
