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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from re import Match, Pattern
from typing import Any, NamedTuple, Union

from sqlalchemy import types
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text
from sqlalchemy.sql.type_api import TypeEngine

from superset.typing import GenericDataType

# (regex, sqla_type_or_factory, generic_data_type)
ColumnTypeMapping = tuple[
    Pattern[str],
    Union[TypeEngine[Any], Callable[[Match[str]], TypeEngine[Any]]],
    GenericDataType,
]


class ColumnSpec(NamedTuple):
    sqla_type: TypeEngine[Any] | str
    generic_type: GenericDataType
    is_dttm: bool
    python_date_format: str | None = None


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

    # SQL expression template converting epoch seconds to datetime.
    # Subclasses override this classmethod with engine-specific SQL.
    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "{col}"

    @classmethod
    def get_datatype(cls, type_code: Any) -> str | None:
        """Map a ``cursor.description`` type code to a string type repr.

        1:1 with ``BaseEngineSpec.get_datatype`` in
        ``superset_old/db_engine_specs/base.py``: string codes (Trino /
        ClickHouse) are upper-cased; non-string codes (e.g. DBAPI int OIDs from
        MySQL) return ``None``. ``AsyncPostgresEngineSpec`` overrides this to
        resolve psycopg2 int OIDs. Defined on the base so every async spec
        exposes it — used by ``SqlaTable._get_virtual_table_metadata``.
        """
        if isinstance(type_code, str) and type_code != "":
            return type_code.upper()
        return None

    # Default column-type mappings used by get_column_types / get_column_spec.
    _default_column_type_mappings: tuple[ColumnTypeMapping, ...] = (  # noqa: RUF012
        (
            re.compile(r"^string", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^n((var)?char|text)", re.IGNORECASE),
            types.UnicodeText(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^(var)?char", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^(tiny|medium|long)?text", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^smallint", re.IGNORECASE),
            types.SmallInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^int(eger)?", re.IGNORECASE),
            types.Integer(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bigint", re.IGNORECASE),
            types.BigInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^long", re.IGNORECASE),
            types.Float(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^decimal", re.IGNORECASE),
            types.Numeric(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^numeric", re.IGNORECASE),
            types.Numeric(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^float", re.IGNORECASE),
            types.Float(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^double", re.IGNORECASE),
            types.Float(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^real", re.IGNORECASE),
            types.REAL(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^smallserial", re.IGNORECASE),
            types.SmallInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^serial", re.IGNORECASE),
            types.Integer(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bigserial", re.IGNORECASE),
            types.BigInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^money", re.IGNORECASE),
            types.Numeric(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^timestamp", re.IGNORECASE),
            types.TIMESTAMP(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^datetime", re.IGNORECASE),
            types.DateTime(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^date", re.IGNORECASE),
            types.Date(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^time", re.IGNORECASE),
            types.Time(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^interval", re.IGNORECASE),
            types.Interval(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^bool(ean)?", re.IGNORECASE),
            types.Boolean(),
            GenericDataType.BOOLEAN,
        ),
    )

    # Engine-specific type mappings checked *before* the defaults.
    # Subclasses override this to handle vendor-specific types.
    column_type_mappings: tuple[ColumnTypeMapping, ...] = ()  # noqa: RUF012

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
        """Return table names in the given schema.

        Uses SQLAlchemy Inspector (same as the original Superset)
        via ``run_sync`` to bridge the async connection.
        """
        from sqlalchemy import inspect as sa_inspect

        def _get(sync_conn: Any) -> set[str]:
            inspector = sa_inspect(sync_conn)
            return set(inspector.get_table_names(schema))

        return await conn.run_sync(_get)

    @classmethod
    async def get_view_names(
        cls,
        conn: AsyncConnection,
        schema: str | None = None,
    ) -> set[str]:
        """Return view names in the given schema.

        Uses SQLAlchemy Inspector via ``run_sync``.
        """
        from sqlalchemy import inspect as sa_inspect

        def _get(sync_conn: Any) -> set[str]:
            inspector = sa_inspect(sync_conn)
            return set(inspector.get_view_names(schema))

        return await conn.run_sync(_get)

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

    # ------------------------------------------------------------------
    # Column-type introspection
    # ------------------------------------------------------------------

    @classmethod
    def get_column_types(
        cls,
        column_type: str | None,
    ) -> tuple[TypeEngine[Any], GenericDataType] | None:
        """Map a native DB column type string to SQLAlchemy + generic types.

        Checks ``column_type_mappings`` (engine-specific) first, then falls
        back to ``_default_column_type_mappings``.

        :param column_type: Column type string returned by the DB inspector.
        :return: ``(sqla_type, generic_type)`` or ``None`` if unrecognised.
        """
        if not column_type:
            return None

        for regex, sqla_type, generic_type in (
            cls.column_type_mappings + cls._default_column_type_mappings
        ):
            match = regex.match(column_type)
            if not match:
                continue
            if callable(sqla_type):
                return sqla_type(match), generic_type
            return sqla_type, generic_type
        return None

    @classmethod
    def get_column_spec(
        cls,
        native_type: str | None,
        db_extra: dict[str, Any] | None = None,
    ) -> ColumnSpec | None:
        """Return a :class:`ColumnSpec` for *native_type*, or ``None``.

        :param native_type: Native database column type string.
        :param db_extra: Optional database extra configuration.
        :return: :class:`ColumnSpec` with ``sqla_type``, ``generic_type``
            and ``is_dttm``, or ``None`` when the type is unrecognised.
        """
        if col_types := cls.get_column_types(native_type):
            column_type, generic_type = col_types
            is_dttm = generic_type == GenericDataType.TEMPORAL
            return ColumnSpec(
                sqla_type=column_type,
                generic_type=generic_type,
                is_dttm=is_dttm,
            )
        return None

    @classmethod
    def get_sqla_column_type(
        cls,
        native_type: str | None,
        db_extra: dict[str, Any] | None = None,
    ) -> TypeEngine[Any] | str | None:
        """Convert a native DB type string to a SQLAlchemy :class:`TypeEngine`.

        Convenience wrapper around :meth:`get_column_spec`.

        :param native_type: Native database column type string.
        :param db_extra: Optional database extra configuration.
        :return: SQLAlchemy type instance or ``None``.
        """
        column_spec = cls.get_column_spec(
            native_type=native_type,
            db_extra=db_extra,
        )
        return column_spec.sqla_type if column_spec else None

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        """Convert a Python ``datetime`` to a SQL expression string.

        Subclasses override to produce engine-specific literals such as
        ``TIMESTAMP '2021-01-01 00:00:00'``.

        :param target_type: Target SQL type (e.g. ``"TIMESTAMP"``).
        :param dttm: The datetime value.
        :param db_extra: Optional database extra configuration.
        :return: SQL expression string, or ``None`` for unsupported types.
        """
        return None
