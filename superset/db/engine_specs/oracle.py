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


class AsyncOracleEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Oracle using oracledb (thin mode) driver."""

    engine = "oracle"
    engine_name = "Oracle"
    default_driver = "oracledb"

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "TO_DATE('1970-01-01','YYYY-MM-DD')+(1/24/60/60)*{col}"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": "CAST({col} as DATE)",
        "PT1M": "TRUNC(CAST({col} as DATE), 'MI')",
        "PT1H": "TRUNC(CAST({col} as DATE), 'HH')",
        "P1D": "TRUNC(CAST({col} as DATE), 'DDD')",
        "P1W": "TRUNC(CAST({col} as DATE), 'WW')",
        "P1M": "TRUNC(CAST({col} as DATE), 'MONTH')",
        "P3M": "TRUNC(CAST({col} as DATE), 'Q')",
        "P1Y": "TRUNC(CAST({col} as DATE), 'YEAR')",
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
            return f"TO_DATE('{dttm.date().isoformat()}', 'YYYY-MM-DD')"
        if isinstance(sqla_type, types.TIMESTAMP):
            return (
                f"TO_TIMESTAMP('{dttm.isoformat(timespec='microseconds')}', "
                "'YYYY-MM-DD\"T\"HH24:MI:SS.ff6')"
            )
        if isinstance(sqla_type, types.DateTime):
            datetime_formatted = dttm.isoformat(timespec="seconds")
            return f"TO_DATE('{datetime_formatted}', 'YYYY-MM-DD\"T\"HH24:MI:SS')"
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
