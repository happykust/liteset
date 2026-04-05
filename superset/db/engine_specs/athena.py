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

from datetime import datetime
from typing import Any

from sqlalchemy import types
from sqlalchemy.ext.asyncio import AsyncConnection

from superset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec


class AsyncAthenaEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Amazon Athena (Presto-like SQL syntax)."""

    engine = "awsathena"
    engine_name = "Amazon Athena"
    default_driver = "rest"

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "from_unixtime({col})"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": "date_trunc('second', CAST({col} AS TIMESTAMP))",
        "PT1M": "date_trunc('minute', CAST({col} AS TIMESTAMP))",
        "PT1H": "date_trunc('hour', CAST({col} AS TIMESTAMP))",
        "P1D": "date_trunc('day', CAST({col} AS TIMESTAMP))",
        "P1W": "date_trunc('week', CAST({col} AS TIMESTAMP))",
        "P1M": "date_trunc('month', CAST({col} AS TIMESTAMP))",
        "P3M": "date_trunc('quarter', CAST({col} AS TIMESTAMP))",
        "P1Y": "date_trunc('year', CAST({col} AS TIMESTAMP))",
        "P1W/1970-01-03T00:00:00Z": (
            "date_add('day', 5, date_trunc('week', "
            "date_add('day', 1, CAST({col} AS TIMESTAMP))))"
        ),
        "1969-12-28T00:00:00Z/P1W": (
            "date_add('day', -1, date_trunc('week', "
            "date_add('day', 1, CAST({col} AS TIMESTAMP))))"
        ),
    }

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"DATE '{dttm.date().isoformat()}'"
        if isinstance(sqla_type, types.TIMESTAMP):
            datetime_formatted = dttm.isoformat(sep=" ", timespec="milliseconds")
            return f"TIMESTAMP '{datetime_formatted}'"
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
