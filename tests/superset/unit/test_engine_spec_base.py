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

import pytest

from superset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec


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
