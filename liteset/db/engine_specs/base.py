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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text


@dataclass(slots=True)
class AsyncResultSet:
    """Result of an async SQL execution."""

    columns: list[str] = field(default_factory=list)
    data: list[tuple[Any, ...]] = field(default_factory=list)
    row_count: int = 0


class BaseAsyncEngineSpec(ABC):
    """Abstract base class for async database engine specifications.

    Provides a simplified async interface for executing SQL queries
    against various database backends. Not a 1:1 copy of the Flask
    BaseEngineSpec — only what's needed for async SQL execution.
    """

    engine: str = ""
    engine_name: str = ""
    default_driver: str = ""
    _time_grain_expressions: dict[str | None, str] = {}  # noqa: RUF012 — safe: __init_subclass__ copies per-subclass; do not mutate base class dict after import
    _custom_errors: list[tuple[re.Pattern[str], str]] = []  # noqa: RUF012

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Ensure each subclass gets its own copy of mutable class-level
        # collections to prevent mutation leaking across the hierarchy.
        if "_time_grain_expressions" not in cls.__dict__:
            cls._time_grain_expressions = dict(cls._time_grain_expressions)
        if "_custom_errors" not in cls.__dict__:
            cls._custom_errors = list(cls._custom_errors)

    @classmethod
    async def _default_execute(
        cls,
        conn: AsyncConnection,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> AsyncResultSet:
        """Default execute implementation for subclasses to reuse."""
        result = await conn.execute(text(query), parameters or {})
        columns = list(result.keys()) if result.returns_rows else []
        data = [tuple(row) for row in result.fetchall()] if result.returns_rows else []
        return AsyncResultSet(
            columns=columns,
            data=data,
            row_count=result.rowcount if result.rowcount >= 0 else len(data),
        )

    @classmethod
    async def _default_fetch_data(
        cls,
        conn: AsyncConnection,
        query: str,
        limit: int | None = None,
    ) -> list[tuple[Any, ...]]:
        """Default fetch_data implementation for subclasses to reuse."""
        result = await conn.execute(text(query))
        if limit is not None:
            return [tuple(row) for row in result.fetchmany(limit)]
        return [tuple(row) for row in result.fetchall()]

    @classmethod
    @abstractmethod
    async def execute(
        cls,
        conn: AsyncConnection,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> AsyncResultSet: ...

    @classmethod
    @abstractmethod
    async def fetch_data(
        cls,
        conn: AsyncConnection,
        query: str,
        limit: int | None = None,
    ) -> list[tuple[Any, ...]]: ...

    @classmethod
    async def get_catalog_names(
        cls,
        conn: AsyncConnection,
    ) -> set[str]:
        """Return available catalog (database) names."""
        result = await conn.execute(
            text("SELECT DISTINCT catalog_name FROM information_schema.schemata")
        )
        return {row[0] for row in result.fetchall()}

    @classmethod
    async def get_schema_names(
        cls,
        conn: AsyncConnection,
        catalog: str | None = None,
    ) -> set[str]:
        """Return available schema names, optionally filtered by catalog."""
        if catalog:
            result = await conn.execute(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE catalog_name = :catalog"
                ),
                {"catalog": catalog},
            )
        else:
            result = await conn.execute(
                text("SELECT schema_name FROM information_schema.schemata")
            )
        return {row[0] for row in result.fetchall()}

    @classmethod
    async def get_table_names(
        cls,
        conn: AsyncConnection,
        schema: str | None = None,
    ) -> set[str]:
        """Return table names in the given schema."""
        if schema:
            result = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema"
                ),
                {"schema": schema},
            )
        else:
            result = await conn.execute(
                text("SELECT table_name FROM information_schema.tables")
            )
        return {row[0] for row in result.fetchall()}

    @classmethod
    async def get_columns(
        cls,
        conn: AsyncConnection,
        table_name: str,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return column metadata for a table."""
        params: dict[str, Any] = {"table_name": table_name}
        q = (
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = :table_name"
        )
        if schema:
            q += " AND table_schema = :schema"
            params["schema"] = schema
        result = await conn.execute(text(q), params)
        return [
            {
                "column_name": row[0],
                "data_type": row[1],
                "is_nullable": row[2] == "YES",
            }
            for row in result.fetchall()
        ]

    @classmethod
    def get_time_grain_expressions(cls) -> dict[str | None, str]:
        """Return time grain expressions for this engine."""
        return cls._time_grain_expressions

    @classmethod
    def extract_errors(cls, ex: Exception) -> list[dict[str, Any]]:
        """Extract structured error information from a database exception.

        Checks _custom_errors patterns first, then falls back to generic.
        """
        error_str = str(ex)
        for pattern, message_template in cls._custom_errors:
            match = pattern.search(error_str)
            if match:
                return [
                    {
                        "message": message_template.format(**match.groupdict()),
                        "error_type": "DatabaseError",
                    }
                ]
        return [{"message": str(ex), "error_type": type(ex).__name__}]

    @classmethod
    def adjust_engine_params(
        cls,
        uri: str,
        connect_args: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Adjust engine connection parameters.

        Subclasses can override to add engine-specific connection args.
        """
        return uri, connect_args or {}
