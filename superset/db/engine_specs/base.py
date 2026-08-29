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
    against various database backends. Not a 1:1 copy of the upstream
    BaseEngineSpec — only what's needed for async SQL execution.
    """

    engine: str = ""
    engine_name: str = ""
    default_driver: str = ""
    _time_grain_expressions: dict[str | None, str] = {}  # noqa: RUF012 — safe: __init_subclass__ copies per-subclass; do not mutate base class dict after import
    _custom_errors: list[tuple[re.Pattern[str], str]] = []  # noqa: RUF012

    enforce_uri_query_params: dict[str, dict[str, Any]] = {}  # noqa: RUF012
    try_remove_schema_from_table_name = True

    @classmethod
    def epoch_to_dttm(cls) -> str:
        raise NotImplementedError()

    @classmethod
    def epoch_ms_to_dttm(cls) -> str:
        return cls.epoch_to_dttm().replace("{col}", "({col}/1000)")

    @classmethod
    def get_datatype(cls, type_code: Any) -> str | None:
        """Map a ``cursor.description`` type code to a string type repr.

        String codes (Trino / ClickHouse) are upper-cased; non-string codes
        (e.g. DBAPI int OIDs from MySQL) return ``None``.
        ``AsyncPostgresEngineSpec`` overrides this to resolve psycopg2 int OIDs.
        Defined on the base so every async spec exposes it — used by
        ``SqlaTable._get_virtual_table_metadata``.
        """
        if isinstance(type_code, str) and type_code != "":
            return type_code.upper()
        return None

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

    column_type_mappings: tuple[ColumnTypeMapping, ...] = ()  # noqa: RUF012
    column_type_mutators: dict[type[TypeEngine[Any]], Callable[[Any], Any]] = {}  # noqa: RUF012

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
        try:
            result = await conn.execute(text(query))

            # Capture cursor description before fetchall() since SA 2.0
            # calls _soft_close() after returning all rows.
            cursor = result.cursor
            description = getattr(cursor, "description", None) or []

            if limit is not None:
                data = [tuple(row) for row in result.fetchmany(limit)]
            else:
                data = [tuple(row) for row in result.fetchall()]

            if cls.column_type_mutators and data:
                column_mutators: dict[int, Callable[[Any], Any]] = {}
                for idx, row in enumerate(description):
                    type_code = row[1]
                    datatype = cls.get_datatype(type_code)
                    sqla_type = cls.get_sqla_column_type(datatype)
                    if sqla_type is not None:
                        func = cls.column_type_mutators.get(
                            type(sqla_type)  # type: ignore[arg-type]
                        )
                        if func is not None:
                            column_mutators[idx] = func

                if column_mutators:
                    for row_idx, row_data in enumerate(data):
                        new_row = list(row_data)
                        for col_idx, func in column_mutators.items():
                            new_row[col_idx] = func(row_data[col_idx])
                        data[row_idx] = tuple(new_row)

            return data
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex

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
        from sqlalchemy import inspect as sa_inspect

        def _get(sync_conn: Any) -> set[str]:
            inspector = sa_inspect(sync_conn)
            return set(inspector.get_table_names(schema))

        try:
            tables = await conn.run_sync(_get)
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex
        if schema and cls.try_remove_schema_from_table_name:
            tables = {re.sub(f"^{schema}\\.", "", table) for table in tables}
        return tables

    @classmethod
    async def get_view_names(
        cls,
        conn: AsyncConnection,
        schema: str | None = None,
    ) -> set[str]:
        from sqlalchemy import inspect as sa_inspect

        def _get(sync_conn: Any) -> set[str]:
            inspector = sa_inspect(sync_conn)
            return set(inspector.get_view_names(schema))

        try:
            views = await conn.run_sync(_get)
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex
        if schema and cls.try_remove_schema_from_table_name:
            views = {re.sub(f"^{schema}\\.", "", view) for view in views}
        return views

    @classmethod
    async def get_columns(
        cls,
        conn: AsyncConnection,
        table_name: str,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
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
    def _sort_time_grains(
        cls, val: tuple[str | None, str], index: int
    ) -> float | int | str:
        """Return an ordered time-based value of a portion of a time grain
        for sorting.
        """
        pos = {
            "FIRST": 0,
            "SECOND": 1,
            "THIRD": 2,
            "LAST": 3,
        }

        if val[0] is None:
            return pos["FIRST"]

        prog = re.compile(r"(.*\/)?(P|PT)([0-9\.]+)(S|M|H|D|W|M|Y)(\/.*)?")
        result = prog.match(val[0])

        # for any time grains that don't match the format, put them at the end
        if result is None:
            return pos["LAST"]

        second_minute_hour = ["S", "M", "H"]
        day_week_month_year = ["D", "W", "M", "Y"]
        is_less_than_day = result.group(2) == "PT"
        interval = result.group(4)
        epoch_time_start_string = result.group(1) or result.group(5)
        has_starting_or_ending = bool(len(epoch_time_start_string or ""))

        def sort_day_week() -> int:
            if has_starting_or_ending:
                return pos["LAST"]
            if is_less_than_day:
                return pos["SECOND"]
            return pos["THIRD"]

        def sort_interval() -> float:
            if is_less_than_day:
                return second_minute_hour.index(interval)
            return day_week_month_year.index(interval)

        # 0: all "PT" values should come before "P" values (i.e, PT10M)
        # 1: order values within the above arrays ("D" before "W")
        # 2: sort by numeric value (PT10M before PT15M)
        # 3: sort by any week starting/ending values
        plist = {
            0: sort_day_week(),
            1: pos["SECOND"] if is_less_than_day else pos["THIRD"],
            2: sort_interval(),
            3: float(result.group(3)),
        }

        return plist.get(index, 0)

    @classmethod
    def get_time_grain_expressions(cls) -> dict[str | None, str]:
        # SupersetSettings() needs required env vars; fall back to defaults
        # when they are absent (e.g. isolated unit-test context).
        grain_addon_expressions: dict[str, dict[str, str]] = {}
        denylist: list[str] = []
        try:
            from pydantic import ValidationError as _PydanticValidationError

            from superset.config import SupersetSettings

            _settings = SupersetSettings()  # type: ignore[call-arg]
            grain_addon_expressions = _settings.time_grain_addon_expressions
            denylist = _settings.time_grain_denylist
        except _PydanticValidationError:
            pass

        time_grain_expressions = cls._time_grain_expressions.copy()
        # grain_addon_expressions values are dict[str, str]; str keys are a valid
        # subset of str | None so we cast rather than re-type the wider annotation.
        from typing import cast

        addon = cast(
            "dict[str | None, str]",
            grain_addon_expressions.get(cls.engine, {}),
        )
        time_grain_expressions.update(addon)
        for key in denylist:
            time_grain_expressions.pop(key, None)

        return dict(
            sorted(
                time_grain_expressions.items(),
                key=lambda x: (
                    cls._sort_time_grains(x, 0),
                    cls._sort_time_grains(x, 1),
                    cls._sort_time_grains(x, 2),
                    cls._sort_time_grains(x, 3),
                ),
            )
        )

    @classmethod
    def get_dbapi_exception_mapping(cls) -> dict[type[Exception], type[Exception]]:
        return {}

    @classmethod
    def parse_error_exception(cls, exception: Exception) -> Exception:
        return exception

    @classmethod
    def get_dbapi_mapped_exception(cls, exception: Exception) -> Exception:
        new_exception = cls.get_dbapi_exception_mapping().get(type(exception))
        if not new_exception:
            return cls.parse_error_exception(exception)
        return new_exception(str(exception))

    @classmethod
    def extract_errors(cls, ex: Exception) -> list[dict[str, Any]]:
        # Route through the sync package's sanitizer so the SQLAlchemy 2.0
        # ``[SQL: ...] [parameters: ...]`` tail and the ``<class '...'>:``
        # DBAPIError-repr prefix are stripped before the message reaches a
        # response body, matching the sync engine-spec path (which already
        # calls this via its own ``extract_errors``). Late-imported to avoid
        # a module-load-time dependency between the async and sync
        # engine-spec packages.
        from superset.db_engine_specs.base import BaseEngineSpec

        error_str = BaseEngineSpec._extract_error_message(ex)
        for pattern, message_template in cls._custom_errors:
            match = pattern.search(error_str)
            if match:
                return [
                    {
                        "message": message_template.format(**match.groupdict()),
                        "error_type": "DatabaseError",
                    }
                ]
        return [{"message": error_str, "error_type": type(ex).__name__}]

    @classmethod
    def adjust_engine_params(
        cls,
        uri: str,
        connect_args: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        from sqlalchemy.engine import make_url

        url = make_url(uri)
        driver = url.get_driver_name()
        return uri, {
            **(connect_args or {}),
            **cls.enforce_uri_query_params.get(driver, {}),
        }

    @classmethod
    def get_column_types(
        cls,
        column_type: str | None,
    ) -> tuple[TypeEngine[Any], GenericDataType] | None:
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
        return None

    @classmethod
    async def estimate_statement_cost(
        cls,
        conn: Any,
        statement: str,
    ) -> dict[str, Any]:
        raise Exception("Database does not support cost estimation")  # noqa: TRY002

    @classmethod
    def estimate_statement_cost_sync(
        cls,
        conn: Any,
        statement: str,
    ) -> dict[str, Any]:
        """Blocking counterpart of :meth:`estimate_statement_cost`.

        Engines without an asyncio driver (Trino, ClickHouse, …) cannot be
        reached through ``create_async_engine``; the estimate command runs
        this over a sync connection in a thread instead.
        """
        raise Exception("Database does not support cost estimation")  # noqa: TRY002
