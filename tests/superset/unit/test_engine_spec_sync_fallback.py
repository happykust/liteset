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

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.db.engine_specs import get_async_engine_spec
from superset.db.engine_specs.base import AsyncResultSet
from superset.db.engine_specs.clickhouse import AsyncClickHouseEngineSpec
from superset.db.engine_specs.mysql import AsyncMySQLEngineSpec
from superset.db.engine_specs.postgres import AsyncPostgresEngineSpec
from superset.db.engine_specs.sync_fallback import (
    _is_overridden,
    make_async_spec,
    SyncFallbackEngineSpec,
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


def test_get_datatype_delegates_to_sync_spec() -> None:
    """get_datatype must delegate to the sync spec so integer DBAPI OID codes
    (e.g. MySQLdb FIELD_TYPE integers) are resolved to type-name strings.
    Without delegation BaseAsyncEngineSpec.get_datatype returns None for
    integer codes, making column_type_mutators permanently inoperative.
    1:1 with BaseEngineSpec.fetch_data calling cls.get_datatype where cls is
    the engine's own spec (superset_old/db_engine_specs/base.py:996).
    """

    class SyncSpecWithIntOids:
        engine = "test_oids"
        engine_name = "TestOIDs"
        # Simulates e.g. MySQLdb FIELD_TYPE: integer OID 246 == DECIMAL
        _type_code_map = {246: "DECIMAL"}

        @classmethod
        def get_datatype(cls, type_code: Any) -> str | None:
            return cls._type_code_map.get(type_code)

    spec = make_async_spec(SyncSpecWithIntOids)
    # Integer OID 246 must resolve to "DECIMAL" via sync spec delegation
    assert spec.get_datatype(246) == "DECIMAL"
    # Unknown OID returns None
    assert spec.get_datatype(999) is None
    # String type codes still work (falls through to sync spec, which
    # delegates to base for non-integer values in a typical override)
    assert spec.get_datatype("VARCHAR") is None  # not in _type_code_map


async def test_fetch_data_column_type_mutators_with_int_oid() -> None:
    """fetch_data must apply column_type_mutators for engines where the DBAPI
    returns integer OID codes in cursor.description.  The fix ensures that
    SyncFallbackEngineSpec.get_datatype delegates to the sync spec's
    get_datatype, matching the original BaseEngineSpec.fetch_data behaviour
    (superset_old/db_engine_specs/base.py:991-998).

    Without the fix, cls.get_datatype(246) returns None (BaseAsyncEngineSpec
    only handles string type codes), so get_sqla_column_type(None) returns None
    and the mutator dict is never consulted.  With the fix, the sync spec's
    get_datatype maps 246 -> "DECIMAL", get_sqla_column_type("DECIMAL") returns
    Numeric() (from _default_column_type_mappings), and the mutator fires.
    """
    from decimal import Decimal

    from sqlalchemy import types as sa_types

    # 246 == MySQLdb FIELD_TYPE.DECIMAL (integer OID used by mysqlclient)
    _oid = 246

    class SyncSpecWithMutators:
        engine = "test_mutators"
        engine_name = "TestMutators"
        # Use Numeric (not DECIMAL) as the key because _default_column_type_mappings
        # maps the "DECIMAL" string to types.Numeric(), not types.DECIMAL().
        column_type_mutators = {
            sa_types.Numeric: lambda val: Decimal(val) if isinstance(val, str) else val,
        }
        _decimal_oid: int = _oid

        @classmethod
        def get_datatype(cls, type_code: Any) -> str | None:
            # Map the integer OID to a type-name string, exactly as
            # MySQLEngineSpec does with MySQLdb.constants.FIELD_TYPE.
            if type_code == cls._decimal_oid:
                return "DECIMAL"
            if isinstance(type_code, str) and type_code:
                return type_code.upper()
            return None

    spec = make_async_spec(SyncSpecWithMutators)

    # cursor.description row: (name, type_code, ...)
    col_desc = [("price", _oid, None, None, None, None, None)]

    mock_cursor = MagicMock()
    mock_cursor.description = col_desc

    mock_result = MagicMock()
    mock_result.cursor = mock_cursor
    mock_result.fetchall.return_value = [("12.50",), ("3.99",)]

    async def fake_run_sync(fn):
        mock_sync_conn = MagicMock()
        mock_sync_conn.execute.return_value = mock_result
        return fn(mock_sync_conn)

    mock_conn = AsyncMock()
    mock_conn.run_sync = fake_run_sync

    rows = await spec.fetch_data(mock_conn, "SELECT price FROM t")
    # Mutator must have converted the string values to Decimal.
    # Before the fix this would be [("12.50",), ("3.99",)] because get_datatype(246)
    # returned None and the mutator was never reached.
    assert rows == [(Decimal("12.50"),), (Decimal("3.99"),)]


async def test_fetch_data_no_mutators_without_column_type_mutators() -> None:
    """Smoke-test: when the sync spec has no column_type_mutators the data is
    returned as-is (no mutation attempted regardless of OID codes)."""

    class SyncSpecNoMutators:
        engine = "test_nomut"
        engine_name = "TestNoMut"

    spec = make_async_spec(SyncSpecNoMutators)

    mock_result = MagicMock()
    mock_result.cursor = None
    mock_result.fetchall.return_value = [("hello",), ("world",)]

    async def fake_run_sync(fn):
        mock_sync_conn = MagicMock()
        mock_sync_conn.execute.return_value = mock_result
        return fn(mock_sync_conn)

    mock_conn = AsyncMock()
    mock_conn.run_sync = fake_run_sync

    rows = await spec.fetch_data(mock_conn, "SELECT x FROM t")
    assert rows == [("hello",), ("world",)]


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

    # Base async get_table_names now uses a SQLAlchemy Inspector via
    # ``conn.run_sync`` (same as upstream), not a raw ``conn.execute(text(...))``.
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.return_value = ["users"]

    async def fake_run_sync(fn):
        return fn(MagicMock())

    mock_conn = AsyncMock()
    mock_conn.run_sync = fake_run_sync

    with patch("sqlalchemy.inspect", return_value=mock_inspector):
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
        with patch(
            "superset.db.engine_specs.sync_fallback.inspect",
            return_value=mock_inspector,
        ):
            return fn(mock_sync_conn)

    mock_conn.run_sync = fake_run_sync
    result = await spec.get_columns(mock_conn, table_name="test_t")
    assert len(result) == 1
    assert result[0]["column_name"] == "col1"


# --- Registry tests ---


def test_registry_raises_for_unknown_engine() -> None:
    import superset.db.engine_specs as registry_mod
    from superset.db.engine_specs import _fallback_cache

    _fallback_cache.pop("totally_unknown_db", None)
    # Reset cached sync spec map so the patched module is used
    registry_mod._sync_spec_map = None

    # Mock the import inside get_async_engine_spec to simulate superset
    # returning no matching spec
    fake_superset_mod = MagicMock()
    fake_superset_mod.load_engine_specs.return_value = []

    with patch.dict(
        "sys.modules",
        {"superset": MagicMock(), "superset.db_engine_specs": fake_superset_mod},
    ):
        with pytest.raises(ValueError, match="No async engine spec found"):
            get_async_engine_spec("totally_unknown_db")

    registry_mod._sync_spec_map = None


def test_registry_creates_fallback_for_sync_spec() -> None:
    import superset.db.engine_specs as registry_mod

    # NOTE: "mssql" now has a native async spec (AsyncMSSQLEngineSpec) in
    # _NATIVE_SPECS, so it is no longer synthesized as a SyncFallback. Use a
    # truly spec-less engine name to exercise the dynamic fallback path.
    engine = "madeup_sync_db"

    class MockSyncSpec:
        engine = "madeup_sync_db"
        engine_name = "Made Up Sync DB"
        default_driver = "madeupdriver"
        _time_grain_expressions = {}

    from superset.db.engine_specs import _fallback_cache

    _fallback_cache.pop(engine, None)
    # Directly inject the mock sync spec into the cached map
    # (bypassing import machinery which doesn't work with patch.dict
    # for subpackage imports)
    registry_mod._sync_spec_map = {engine: MockSyncSpec}

    spec = get_async_engine_spec(engine)
    assert spec.engine == engine
    assert issubclass(spec, SyncFallbackEngineSpec)
    # Second call should hit cache
    spec2 = get_async_engine_spec(engine)
    assert spec2 is spec

    _fallback_cache.pop(engine, None)
    registry_mod._sync_spec_map = None
