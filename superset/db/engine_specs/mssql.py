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
from datetime import datetime
from typing import Any

from sqlalchemy import types
from sqlalchemy.ext.asyncio import AsyncConnection

from superset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec
from superset.typing import GenericDataType


class AsyncMSSQLEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Microsoft SQL Server using aioodbc driver."""

    engine = "mssql"
    engine_name = "Microsoft SQL Server"
    default_driver = "aioodbc"

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "dateadd(S, {col}, '1970-01-01')"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": (
            "DATEADD(SECOND, DATEDIFF(SECOND, '2000-01-01', {col}), '2000-01-01')"
        ),
        "PT1M": "DATEADD(MINUTE, DATEDIFF(MINUTE, 0, {col}), 0)",
        "PT5M": "DATEADD(MINUTE, DATEDIFF(MINUTE, 0, {col}) / 5 * 5, 0)",
        "PT10M": "DATEADD(MINUTE, DATEDIFF(MINUTE, 0, {col}) / 10 * 10, 0)",
        "PT15M": "DATEADD(MINUTE, DATEDIFF(MINUTE, 0, {col}) / 15 * 15, 0)",
        "PT30M": "DATEADD(MINUTE, DATEDIFF(MINUTE, 0, {col}) / 30 * 30, 0)",
        "PT1H": "DATEADD(HOUR, DATEDIFF(HOUR, 0, {col}), 0)",
        "P1D": "DATEADD(DAY, DATEDIFF(DAY, 0, {col}), 0)",
        "P1W": (
            "DATEADD(DAY, 1 - DATEPART(WEEKDAY, {col}),"
            " DATEADD(DAY, DATEDIFF(DAY, 0, {col}), 0))"
        ),
        "P1M": "DATEADD(MONTH, DATEDIFF(MONTH, 0, {col}), 0)",
        "P3M": "DATEADD(QUARTER, DATEDIFF(QUARTER, 0, {col}), 0)",
        "P1Y": "DATEADD(YEAR, DATEDIFF(YEAR, 0, {col}), 0)",
        "1969-12-28T00:00:00Z/P1W": (
            "DATEADD(DAY, -1, DATEADD(WEEK, DATEDIFF(WEEK, 0, {col}), 0))"
        ),
        "1969-12-29T00:00:00Z/P1W": (
            "DATEADD(WEEK, DATEDIFF(WEEK, 0, DATEADD(DAY, -1, {col})), 0)"
        ),
    }

    column_type_mappings: Any = (
        (
            re.compile(r"^smalldatetime.*", re.IGNORECASE),
            types.DateTime(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^uniqueidentifier.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
    )

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"CONVERT(DATE, '{dttm.date().isoformat()}', 23)"
        if isinstance(sqla_type, types.DateTime):
            # Check for smalldatetime (seconds precision)
            if target_type.upper().startswith("SMALLDATETIME"):
                datetime_formatted = dttm.isoformat(sep=" ", timespec="seconds")
                return f"CONVERT(SMALLDATETIME, '{datetime_formatted}', 20)"
            datetime_formatted = dttm.isoformat(timespec="milliseconds")
            return f"CONVERT(DATETIME, '{datetime_formatted}', 126)"
        return None

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
