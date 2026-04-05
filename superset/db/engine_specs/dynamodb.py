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

from superset.constants import TimeGrain
from superset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec


class AsyncDynamoDBEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Amazon DynamoDB.

    DynamoDB is a NoSQL database with limited SQL support via PartiQL.
    Uses SQLite-style date functions for time grain expressions.
    """

    engine = "dynamodb"
    engine_name = "Amazon DynamoDB"
    default_driver = "aioboto3"

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "datetime({col}, 'unixepoch')"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        TimeGrain.SECOND: "DATETIME(STRFTIME('%Y-%m-%dT%H:%M:%S', {col}))",
        TimeGrain.MINUTE: "DATETIME(STRFTIME('%Y-%m-%dT%H:%M:00', {col}))",
        TimeGrain.HOUR: "DATETIME(STRFTIME('%Y-%m-%dT%H:00:00', {col}))",
        TimeGrain.DAY: "DATETIME({col}, 'start of day')",
        TimeGrain.WEEK: (
            "DATETIME({col}, 'start of day', -strftime('%w', {col}) || ' days')"
        ),
        TimeGrain.MONTH: "DATETIME({col}, 'start of month')",
        TimeGrain.QUARTER: (
            "DATETIME({col}, 'start of month', "
            "printf('-%d month', (strftime('%m', {col}) - 1) % 3))"
        ),
        TimeGrain.YEAR: "DATETIME({col}, 'start of year')",
        TimeGrain.WEEK_ENDING_SATURDAY: (
            "DATETIME({col}, 'start of day', 'weekday 6')"
        ),
        TimeGrain.WEEK_ENDING_SUNDAY: ("DATETIME({col}, 'start of day', 'weekday 0')"),
        TimeGrain.WEEK_STARTING_SUNDAY: (
            "DATETIME({col}, 'start of day', 'weekday 0', '-7 days')"
        ),
        TimeGrain.WEEK_STARTING_MONDAY: (
            "DATETIME({col}, 'start of day', 'weekday 1', '-7 days')"
        ),
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
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, (types.String, types.DateTime)):
            return f"""'{dttm.isoformat(sep=" ", timespec="seconds")}'"""

        return None
