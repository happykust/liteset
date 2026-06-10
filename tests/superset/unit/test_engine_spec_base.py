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

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from superset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec
from superset.db_engine_specs.exceptions import (
    SupersetDBAPIConnectionError,
    SupersetDBAPIOperationalError,
)


def _make_concrete(cls: type) -> type:
    """Add concrete execute/fetch_data to a BaseAsyncEngineSpec subclass."""
    if "execute" not in cls.__dict__:

        @classmethod  # type: ignore[misc]
        async def execute(c, conn, query, parameters=None):
            return await c._default_execute(conn, query, parameters)

        cls.execute = execute
    if "fetch_data" not in cls.__dict__:

        @classmethod  # type: ignore[misc]
        async def fetch_data(c, conn, query, limit=None):
            return await c._default_fetch_data(conn, query, limit)

        cls.fetch_data = fetch_data
    return cls


def test_async_result_set_defaults() -> None:
    rs = AsyncResultSet()
    assert rs.columns == []
    assert rs.data == []
    assert rs.row_count == 0


def test_async_result_set_with_data() -> None:
    rs = AsyncResultSet(
        columns=["id", "name"],
        data=[(1, "alice"), (2, "bob")],
        row_count=2,
    )
    assert rs.columns == ["id", "name"]
    assert len(rs.data) == 2
    assert rs.row_count == 2


def test_get_time_grain_expressions_empty() -> None:
    """Base class returns empty time grain expressions."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        pass

    assert StubSpec.get_time_grain_expressions() == {}


def test_get_time_grain_expressions_subclass() -> None:
    """Subclass time grains are returned correctly."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        _time_grain_expressions = {
            None: "{col}",
            "P1D": "DATE_TRUNC('day', {col})",
        }

    result = StubSpec.get_time_grain_expressions()
    assert result[None] == "{col}"
    assert result["P1D"] == "DATE_TRUNC('day', {col})"


def test_get_time_grain_expressions_no_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_time_grain_expressions must not require LITESET_SECRET_KEY or
    LITESET_SQLALCHEMY_DATABASE_URI to be set.

    Regression guard: the original BaseEngineSpec.get_time_grain_expressions
    (superset_old/db_engine_specs/base.py:946-971) read app.config directly
    and had zero configuration preconditions.  The liteset port must not add
    them — callers (CLI tools, schema-inspection utilities, unit tests) must
    be able to call the method without a fully initialised SupersetSettings.
    """
    # Strip the required env vars so that SupersetSettings() would raise
    # pydantic.ValidationError if called unconditionally.
    monkeypatch.delenv("LITESET_SECRET_KEY", raising=False)
    monkeypatch.delenv("LITESET_SQLALCHEMY_DATABASE_URI", raising=False)
    # Also strip legacy env vars that could satisfy the required fields.
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("SQLALCHEMY_DATABASE_URI", raising=False)
    monkeypatch.delenv("SUPERSET_SECRET_KEY", raising=False)

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        engine = "testengine"
        _time_grain_expressions = {"P1D": "TRUNC({col}, 'DAY')"}

    # Must not raise; addon/denylist default to {} / [] when settings unavailable.
    result = StubSpec.get_time_grain_expressions()
    assert result == {"P1D": "TRUNC({col}, 'DAY')"}


def test_extract_errors_default() -> None:
    """Default extract_errors returns message and type."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        pass

    errors = StubSpec.extract_errors(ValueError("something broke"))
    assert len(errors) == 1
    assert errors[0]["message"] == "something broke"
    assert errors[0]["error_type"] == "ValueError"


def test_adjust_engine_params_default() -> None:
    """Default adjust_engine_params passes through uri and connect_args."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        pass

    uri, args = StubSpec.adjust_engine_params("postgresql://localhost/db")
    assert uri == "postgresql://localhost/db"
    assert args == {}

    uri2, args2 = StubSpec.adjust_engine_params(
        "postgresql://localhost/db", {"sslmode": "require"}
    )
    assert args2 == {"sslmode": "require"}


def test_class_attributes_default() -> None:
    """Base class has sensible defaults for class attributes."""
    assert BaseAsyncEngineSpec.engine == ""
    assert BaseAsyncEngineSpec.engine_name == ""
    assert BaseAsyncEngineSpec.default_driver == ""


def test_time_grain_isolation_between_subclasses() -> None:
    """Mutating _time_grain_expressions in one subclass doesn't affect another."""

    @_make_concrete
    class SpecA(BaseAsyncEngineSpec):
        _time_grain_expressions = {"P1D": "a"}

    @_make_concrete
    class SpecB(BaseAsyncEngineSpec):
        _time_grain_expressions = {"P1D": "b"}

    SpecA._time_grain_expressions["P1W"] = "a_week"
    assert "P1W" not in SpecB._time_grain_expressions
    assert "P1W" not in BaseAsyncEngineSpec._time_grain_expressions


def test_cannot_instantiate_base_directly() -> None:
    """BaseAsyncEngineSpec cannot be instantiated without implementing abstract
    methods.
    """

    # Subclass without execute/fetch_data — instantiation must fail.
    class IncompleteSpec(BaseAsyncEngineSpec):
        pass

    with pytest.raises(TypeError):
        IncompleteSpec()


async def test_execute_returns_result_set() -> None:
    """ConcreteSpec.execute returns AsyncResultSet via aiosqlite."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.sql import text as sa_text

    @_make_concrete
    class ConcreteSpec(BaseAsyncEngineSpec):
        engine = "sqlite"

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.execute(sa_text("CREATE TABLE test_t (id INTEGER, name TEXT)"))
        await conn.execute(sa_text("INSERT INTO test_t VALUES (1, 'a'), (2, 'b')"))
    async with engine.connect() as conn:
        rs = await ConcreteSpec.execute(conn, "SELECT * FROM test_t ORDER BY id")
        assert rs.columns == ["id", "name"]
        assert rs.data == [(1, "a"), (2, "b")]
        assert rs.row_count == 2
    await engine.dispose()


async def test_fetch_data_returns_rows() -> None:
    """ConcreteSpec.fetch_data returns raw tuples."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.sql import text as sa_text

    @_make_concrete
    class ConcreteSpec(BaseAsyncEngineSpec):
        engine = "sqlite"

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.execute(sa_text("CREATE TABLE test_t2 (id INTEGER, val TEXT)"))
        await conn.execute(
            sa_text("INSERT INTO test_t2 VALUES (1, 'x'), (2, 'y'), (3, 'z')")
        )
    async with engine.connect() as conn:
        rows = await ConcreteSpec.fetch_data(conn, "SELECT * FROM test_t2 ORDER BY id")
        assert len(rows) == 3
        assert rows[0] == (1, "x")
    await engine.dispose()


async def test_fetch_data_with_limit() -> None:
    """ConcreteSpec.fetch_data respects limit."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.sql import text as sa_text

    @_make_concrete
    class ConcreteSpec(BaseAsyncEngineSpec):
        engine = "sqlite"

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.execute(sa_text("CREATE TABLE test_t3 (id INTEGER)"))
        await conn.execute(sa_text("INSERT INTO test_t3 VALUES (1), (2), (3)"))
    async with engine.connect() as conn:
        rows = await ConcreteSpec.fetch_data(conn, "SELECT * FROM test_t3", limit=2)
        assert len(rows) == 2
    await engine.dispose()


def test_custom_errors_isolation_between_subclasses() -> None:
    """Mutating _custom_errors in one subclass doesn't affect another."""

    @_make_concrete
    class SpecX(BaseAsyncEngineSpec):
        _custom_errors = [(re.compile(r"foo"), "Foo error")]

    @_make_concrete
    class SpecY(BaseAsyncEngineSpec):
        _custom_errors = [(re.compile(r"bar"), "Bar error")]

    SpecX._custom_errors.append((re.compile(r"baz"), "Baz error"))
    assert len(SpecY._custom_errors) == 1
    assert len(BaseAsyncEngineSpec._custom_errors) == 0


# ---------------------------------------------------------------------------
# DBAPI exception mapping — 1:1 with BaseEngineSpec (superset_old lines 751–786)
# ---------------------------------------------------------------------------


def test_get_dbapi_exception_mapping_returns_empty_by_default() -> None:
    """Base class returns an empty mapping, identical to the original."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        pass

    assert StubSpec.get_dbapi_exception_mapping() == {}


def test_parse_error_exception_returns_same_exception() -> None:
    """parse_error_exception returns the original exception unchanged by default."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        pass

    exc = ValueError("test error")
    assert StubSpec.parse_error_exception(exc) is exc


def test_get_dbapi_mapped_exception_falls_back_to_parse_when_no_mapping() -> None:
    """No mapping: get_dbapi_mapped_exception returns parse_error_exception result."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        pass

    exc = RuntimeError("db error")
    result = StubSpec.get_dbapi_mapped_exception(exc)
    # Default parse_error_exception is identity — same object returned
    assert result is exc


def test_get_dbapi_mapped_exception_uses_mapping() -> None:
    """When a mapping exists, get_dbapi_mapped_exception wraps into the mapped type."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        @classmethod
        def get_dbapi_exception_mapping(
            cls,
        ) -> dict[type[Exception], type[Exception]]:
            return {ConnectionError: SupersetDBAPIConnectionError}

    exc = ConnectionError("connection refused")
    result = StubSpec.get_dbapi_mapped_exception(exc)
    assert isinstance(result, SupersetDBAPIConnectionError)
    assert "connection refused" in str(result)


def test_get_dbapi_mapped_exception_subclass_override() -> None:
    """Subclass parse_error_exception is used when no direct mapping matches."""

    class MyOperationalError(RuntimeError):
        pass

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        @classmethod
        def get_dbapi_exception_mapping(
            cls,
        ) -> dict[type[Exception], type[Exception]]:
            return {MyOperationalError: SupersetDBAPIOperationalError}

        @classmethod
        def parse_error_exception(cls, exception: Exception) -> Exception:
            return ValueError(f"parsed: {exception}")

    # Exact type in mapping → mapped exception
    exc1 = MyOperationalError("op error")
    result1 = StubSpec.get_dbapi_mapped_exception(exc1)
    assert isinstance(result1, SupersetDBAPIOperationalError)

    # Unknown type → falls back to parse_error_exception
    exc2 = RuntimeError("generic error")
    result2 = StubSpec.get_dbapi_mapped_exception(exc2)
    assert isinstance(result2, ValueError)
    assert "parsed: generic error" in str(result2)


# ---------------------------------------------------------------------------
# get_table_names / get_view_names — exception wrapping (regression guard)
# 1:1 with BaseEngineSpec.get_table_names superset_old/db_engine_specs/base.py:1459-1466
# ---------------------------------------------------------------------------


async def test_get_table_names_wraps_inspector_exception() -> None:
    """Inspector errors are mapped via get_dbapi_mapped_exception.

    1:1 with BaseEngineSpec.get_table_names (superset_old lines 1459-1462).
    """

    class MyDriverError(Exception):
        pass

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        @classmethod
        def get_dbapi_exception_mapping(
            cls,
        ) -> dict[type[Exception], type[Exception]]:
            return {MyDriverError: SupersetDBAPIOperationalError}

    # Build a fake async connection whose run_sync raises MyDriverError
    fake_conn = MagicMock()
    fake_conn.run_sync = AsyncMock(side_effect=MyDriverError("schema not found"))

    with pytest.raises(SupersetDBAPIOperationalError, match="schema not found"):
        await StubSpec.get_table_names(fake_conn, schema="bad_schema")


async def test_get_table_names_passes_through_on_success() -> None:
    """get_table_names returns names from inspector when no error occurs."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        try_remove_schema_from_table_name = False

    fake_conn = MagicMock()
    fake_conn.run_sync = AsyncMock(return_value={"table_a", "table_b"})

    result = await StubSpec.get_table_names(fake_conn, schema=None)
    assert result == {"table_a", "table_b"}


async def test_get_view_names_wraps_inspector_exception() -> None:
    """Inspector errors are mapped via get_dbapi_mapped_exception.

    1:1 with BaseEngineSpec.get_view_names (superset_old lines 1487-1490).
    """

    class MyDriverError(Exception):
        pass

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        @classmethod
        def get_dbapi_exception_mapping(
            cls,
        ) -> dict[type[Exception], type[Exception]]:
            return {MyDriverError: SupersetDBAPIConnectionError}

    fake_conn = MagicMock()
    fake_conn.run_sync = AsyncMock(side_effect=MyDriverError("connection lost"))

    with pytest.raises(SupersetDBAPIConnectionError, match="connection lost"):
        await StubSpec.get_view_names(fake_conn, schema=None)


async def test_get_view_names_passes_through_on_success() -> None:
    """get_view_names returns names from inspector when no error occurs."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        try_remove_schema_from_table_name = False

    fake_conn = MagicMock()
    fake_conn.run_sync = AsyncMock(return_value={"view_x", "view_y"})

    result = await StubSpec.get_view_names(fake_conn, schema=None)
    assert result == {"view_x", "view_y"}


async def test_get_table_names_unmapped_exception_propagates_as_original_type() -> None:
    """Unmapped exceptions propagate via parse_error_exception (identity by default)."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        pass

    fake_conn = MagicMock()
    fake_conn.run_sync = AsyncMock(side_effect=OSError("disk error"))

    with pytest.raises(OSError, match="disk error"):
        await StubSpec.get_table_names(fake_conn, schema=None)


async def test_get_view_names_unmapped_exception_propagates_as_original_type() -> None:
    """Unmapped exceptions propagate via parse_error_exception (identity by default)."""

    @_make_concrete
    class StubSpec(BaseAsyncEngineSpec):
        pass

    fake_conn = MagicMock()
    fake_conn.run_sync = AsyncMock(side_effect=OSError("network error"))

    with pytest.raises(OSError, match="network error"):
        await StubSpec.get_view_names(fake_conn, schema=None)


# ---------------------------------------------------------------------------
# BaseEngineSpec.execute_with_cursor — cancel-query-id session commit
# Regression guard: object_session(query) must be None-guarded before commit.
# Original (superset_old/db_engine_specs/base.py:1316): db.session.commit()
#   — Flask-SQLAlchemy scoped session, always non-None in WSGI context.
# Liteset port: object_session(query) can return None for detached/transient
#   objects; missing None-guard would raise AttributeError on None.commit().
# ---------------------------------------------------------------------------


def test_execute_with_cursor_commits_cancel_query_id(
    mocker: MockerFixture,
) -> None:
    """When get_cancel_query_id returns a non-None value, execute_with_cursor
    must call session.commit() — mirrors the original db.session.commit()."""
    from superset.db_engine_specs.base import BaseEngineSpec

    cursor = mocker.MagicMock()
    query = mocker.MagicMock()
    query.id = 42
    # get_cancel_query_id is called via query.database.db_engine_spec
    query.database.db_engine_spec.get_cancel_query_id.return_value = "cid-123"

    mock_session = mocker.MagicMock()
    mocker.patch("sqlalchemy.orm.object_session", return_value=mock_session)
    mocker.patch.object(BaseEngineSpec, "execute")
    mocker.patch.object(BaseEngineSpec, "handle_cursor")
    mocker.patch.object(BaseEngineSpec, "has_query_id_before_execute", False)

    BaseEngineSpec.execute_with_cursor(cursor, "SELECT 1", query)

    query.set_extra_json_key.assert_called_once()
    mock_session.commit.assert_called_once()


def test_execute_with_cursor_no_crash_when_session_is_none(
    mocker: MockerFixture,
) -> None:
    """When object_session(query) returns None (detached/transient object),
    execute_with_cursor must NOT raise AttributeError — the None guard protects
    the session.commit() call."""
    from superset.db_engine_specs.base import BaseEngineSpec

    cursor = mocker.MagicMock()
    query = mocker.MagicMock()
    query.id = 99
    query.database.db_engine_spec.get_cancel_query_id.return_value = "cid-xyz"

    mocker.patch("sqlalchemy.orm.object_session", return_value=None)
    mocker.patch.object(BaseEngineSpec, "execute")
    mocker.patch.object(BaseEngineSpec, "handle_cursor")
    mocker.patch.object(BaseEngineSpec, "has_query_id_before_execute", False)

    # Must not raise AttributeError ("'NoneType' object has no attribute 'commit'")
    BaseEngineSpec.execute_with_cursor(cursor, "SELECT 1", query)

    # cancel id was still stored on the model even when session is None
    query.set_extra_json_key.assert_called_once()


def test_execute_with_cursor_skips_commit_when_no_cancel_query_id(
    mocker: MockerFixture,
) -> None:
    """When get_cancel_query_id returns None, no object_session lookup or commit
    should occur — mirrors the original if cancel_query_id is not None: guard."""
    from superset.db_engine_specs.base import BaseEngineSpec

    cursor = mocker.MagicMock()
    query = mocker.MagicMock()
    query.id = 7
    # Returning None means the cancel-id block is skipped entirely
    query.database.db_engine_spec.get_cancel_query_id.return_value = None

    mock_session = mocker.MagicMock()
    object_session_mock = mocker.patch(
        "sqlalchemy.orm.object_session", return_value=mock_session
    )
    mocker.patch.object(BaseEngineSpec, "execute")
    mocker.patch.object(BaseEngineSpec, "handle_cursor")
    mocker.patch.object(BaseEngineSpec, "has_query_id_before_execute", False)

    BaseEngineSpec.execute_with_cursor(cursor, "SELECT 1", query)

    object_session_mock.assert_not_called()
    mock_session.commit.assert_not_called()


async def test_fetch_data_mutators_use_description_captured_before_fetchall() -> None:
    """``cursor.description`` must be read BEFORE ``fetchall()``.

    SA 2.0 ``CursorFetchStrategy.fetchall`` calls ``result._soft_close()``
    which sets ``result.cursor = None`` — reading the description afterwards
    crashes with AttributeError for any spec with ``column_type_mutators``
    (e.g. MySQL DECIMAL). Mirrors the documented capture in
    sync_fallback.py:182-186.
    """
    from decimal import Decimal
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import DECIMAL

    @_make_concrete
    class MutatingSpec(BaseAsyncEngineSpec):
        engine = "sqlite"
        column_type_mutators = {DECIMAL: lambda val: Decimal(val) * 2}

        @classmethod
        def get_datatype(cls, type_code):
            return "DECIMAL"

        @classmethod
        def get_sqla_column_type(cls, datatype):
            return DECIMAL()

    result = MagicMock()
    cursor = MagicMock()
    cursor.description = [("amount", 246, None, None, None, None, None)]
    result.cursor = cursor

    def _fetchall():
        # Simulate SA 2.0 _soft_close(): cursor is gone after fetchall.
        result.cursor = None
        return [(Decimal("1.5"),), (Decimal("2.5"),)]

    result.fetchall = MagicMock(side_effect=_fetchall)
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=result)

    rows = await MutatingSpec.fetch_data(conn, "SELECT amount FROM t")
    assert rows == [(Decimal("3.0"),), (Decimal("5.0"),)]
