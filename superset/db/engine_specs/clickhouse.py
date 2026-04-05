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

import logging
import re
from datetime import datetime
from enum import IntEnum
from typing import Any

from sqlalchemy import types
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text

from superset.db.engine_specs.base import (
    AsyncResultSet,
    BaseAsyncEngineSpec,
    ColumnTypeMapping,
)
from superset.typing import GenericDataType

logger = logging.getLogger(__name__)


class _GenericDataType(IntEnum):
    """Subset of GenericDataType used by column type mappings."""

    NUMERIC = 0
    STRING = 1
    TEMPORAL = 2
    BOOLEAN = 3


_VALID_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_\-]*$")


class AsyncClickHouseEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for ClickHouse using asynch driver."""

    engine = "clickhouse"
    engine_name = "ClickHouse"
    default_driver = "asynch"

    time_groupby_inline = True

    column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r".*Enum.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*Array.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*UUID.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*Bool.*", re.IGNORECASE),
            types.Boolean(),
            GenericDataType.BOOLEAN,
        ),
        (
            re.compile(r".*String.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*Int\d+.*", re.IGNORECASE),
            types.INTEGER(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r".*Decimal.*", re.IGNORECASE),
            types.DECIMAL(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r".*DateTime.*", re.IGNORECASE),
            types.DateTime(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r".*Date.*", re.IGNORECASE),
            types.Date(),
            GenericDataType.TEMPORAL,
        ),
    )

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        # toStartOfSecond requires ClickHouse >= 20.4; not in original superset spec,
        # added as enhancement since modern ClickHouse versions support it.
        "PT1S": "toStartOfSecond({col})",
        "PT1M": "toStartOfMinute(toDateTime({col}))",
        "PT5M": "toDateTime(intDiv(toUInt32(toDateTime({col})), 300)*300)",
        "PT10M": "toDateTime(intDiv(toUInt32(toDateTime({col})), 600)*600)",
        "PT15M": "toDateTime(intDiv(toUInt32(toDateTime({col})), 900)*900)",
        "PT30M": "toDateTime(intDiv(toUInt32(toDateTime({col})), 1800)*1800)",
        "PT1H": "toStartOfHour(toDateTime({col}))",
        "P1D": "toStartOfDay(toDateTime({col}))",
        "P1W": "toMonday(toDateTime({col}))",
        "P1M": "toStartOfMonth(toDateTime({col}))",
        "P3M": "toStartOfQuarter(toDateTime({col}))",
        "P1Y": "toStartOfYear(toDateTime({col}))",
    }

    @classmethod
    async def execute(
        cls,
        conn: AsyncConnection,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> AsyncResultSet:
        return await cls._default_execute(conn, query, parameters)

    @classmethod
    async def fetch_data(
        cls,
        conn: AsyncConnection,
        query: str,
        limit: int | None = None,
    ) -> list[tuple[Any, ...]]:
        return await cls._default_fetch_data(conn, query, limit)

    @classmethod
    async def get_schema_names(
        cls,
        conn: AsyncConnection,
        catalog: str | None = None,
    ) -> set[str]:
        result = await conn.execute(text("SHOW DATABASES"))
        return {row[0] for row in result.fetchall()}

    @classmethod
    async def get_table_names(
        cls,
        conn: AsyncConnection,
        schema: str | None = None,
    ) -> set[str]:
        if schema:
            if not _VALID_IDENTIFIER.match(schema):
                raise ValueError(f"Invalid schema identifier: {schema}")
            result = await conn.execute(text(f"SHOW TABLES FROM `{schema}`"))
        else:
            result = await conn.execute(text("SHOW TABLES"))
        return {row[0] for row in result.fetchall()}

    @classmethod
    def adjust_engine_params(
        cls,
        uri: str,
        connect_args: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        args = connect_args.copy() if connect_args else {}
        args.setdefault("connect_timeout", 10)
        args.setdefault("send_receive_timeout", 300)
        return uri, args

    @classmethod
    def epoch_to_dttm(cls) -> str:
        """ClickHouse stores epoch timestamps as-is."""
        return "{col}"

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        """Convert a Python datetime to a ClickHouse date/datetime literal."""
        sqla_type = cls._get_sqla_column_type(target_type)
        if isinstance(sqla_type, types.Date):
            return f"toDate('{dttm.date().isoformat()}')"
        if isinstance(sqla_type, types.DateTime):
            return f"""toDateTime('{dttm.isoformat(sep=" ", timespec="seconds")}')"""
        return None

    @classmethod
    def get_dbapi_exception_mapping(cls) -> dict[type[Exception], type[Exception]]:
        """Map urllib3 NewConnectionError to a generic ConnectionError."""
        try:
            from urllib3.exceptions import NewConnectionError
        except ImportError:
            return {}
        return {NewConnectionError: ConnectionError}

    @classmethod
    async def get_function_names(cls, conn: AsyncConnection) -> list[str]:
        """Query system.functions for SQL Lab autocomplete.

        :param conn: An async database connection
        :return: A list of function names usable in the database
        """
        system_functions_sql = "SELECT name FROM system.functions"
        try:
            result = await conn.execute(text(system_functions_sql))
            rows = result.fetchall()
            return [row[0] for row in rows]
        except Exception:
            logger.exception(
                "Error fetching function names from system.functions",
            )
            return []

    @staticmethod
    def _get_sqla_column_type(native_type: str) -> types.TypeEngine[Any]:
        """Resolve a ClickHouse type string to a SQLAlchemy type instance."""
        type_map: list[tuple[re.Pattern[str], types.TypeEngine[Any]]] = [
            (re.compile(r".*DateTime.*", re.IGNORECASE), types.DateTime()),
            (re.compile(r".*Date.*", re.IGNORECASE), types.Date()),
            (re.compile(r".*String.*", re.IGNORECASE), types.String()),
            (re.compile(r".*Int\d+.*", re.IGNORECASE), types.INTEGER()),
            (re.compile(r".*Decimal.*", re.IGNORECASE), types.DECIMAL()),
            (re.compile(r".*Bool.*", re.IGNORECASE), types.Boolean()),
        ]
        for pattern, sqla_type in type_map:
            if pattern.match(native_type):
                return sqla_type
        return types.String()
