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

from sqlalchemy.ext.asyncio import AsyncConnection

from superset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec


class AsyncPinotEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Apache Pinot."""

    engine = "pinot"
    engine_name = "Apache Pinot"
    default_driver = "http"

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return (
            "DATETIMECONVERT({col}, '1:SECONDS:EPOCH', '1:SECONDS:EPOCH', '1:SECONDS')"
        )

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": ("CAST(DATE_TRUNC('second', CAST({col} AS TIMESTAMP)) AS TIMESTAMP)"),
        "PT1M": ("CAST(DATE_TRUNC('minute', CAST({col} AS TIMESTAMP)) AS TIMESTAMP)"),
        "PT5M": (
            "CAST(ROUND(DATE_TRUNC('minute', "
            "CAST({col} AS TIMESTAMP)), 300000) AS TIMESTAMP)"
        ),
        "PT10M": (
            "CAST(ROUND(DATE_TRUNC('minute', "
            "CAST({col} AS TIMESTAMP)), 600000) AS TIMESTAMP)"
        ),
        "PT15M": (
            "CAST(ROUND(DATE_TRUNC('minute', "
            "CAST({col} AS TIMESTAMP)), 900000) AS TIMESTAMP)"
        ),
        "PT30M": (
            "CAST(ROUND(DATE_TRUNC('minute', "
            "CAST({col} AS TIMESTAMP)), 1800000) AS TIMESTAMP)"
        ),
        "PT1H": ("CAST(DATE_TRUNC('hour', CAST({col} AS TIMESTAMP)) AS TIMESTAMP)"),
        "P1D": ("CAST(DATE_TRUNC('day', CAST({col} AS TIMESTAMP)) AS TIMESTAMP)"),
        "P1W": ("CAST(DATE_TRUNC('week', CAST({col} AS TIMESTAMP)) AS TIMESTAMP)"),
        "P1M": ("CAST(DATE_TRUNC('month', CAST({col} AS TIMESTAMP)) AS TIMESTAMP)"),
        "P3M": ("CAST(DATE_TRUNC('quarter', CAST({col} AS TIMESTAMP)) AS TIMESTAMP)"),
        "P1Y": ("CAST(DATE_TRUNC('year', CAST({col} AS TIMESTAMP)) AS TIMESTAMP)"),
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
