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


class AsyncYDBEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for YDB (Yandex Database).

    Ports time grain expressions, epoch_to_dttm, and convert_dttm from
    the legacy YDBEngineSpec. Uses YDB's DateTime:: UDF module.
    """

    engine = "yql"
    engine_name = "YDB"
    default_driver = "ydb"

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "DateTime::MakeDatetime({col})"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        TimeGrain.SECOND: (
            "DateTime::MakeDatetime(DateTime::StartOf({col}, Interval('PT1S')))"
        ),
        TimeGrain.THIRTY_SECONDS: (
            "DateTime::MakeDatetime(DateTime::StartOf({col}, Interval('PT30S')))"
        ),
        TimeGrain.MINUTE: (
            "DateTime::MakeDatetime(DateTime::StartOf({col}, Interval('PT1M')))"
        ),
        TimeGrain.FIVE_MINUTES: (
            "DateTime::MakeDatetime(DateTime::StartOf({col}, Interval('PT5M')))"
        ),
        TimeGrain.TEN_MINUTES: (
            "DateTime::MakeDatetime(DateTime::StartOf({col}, Interval('PT10M')))"
        ),
        TimeGrain.FIFTEEN_MINUTES: (
            "DateTime::MakeDatetime(DateTime::StartOf({col}, Interval('PT15M')))"
        ),
        TimeGrain.THIRTY_MINUTES: (
            "DateTime::MakeDatetime(DateTime::StartOf({col}, Interval('PT30M')))"
        ),
        TimeGrain.HOUR: (
            "DateTime::MakeDatetime(DateTime::StartOf({col}, Interval('PT1H')))"
        ),
        TimeGrain.DAY: (
            "DateTime::MakeDatetime(DateTime::StartOf({col}, Interval('P1D')))"
        ),
        TimeGrain.WEEK: "DateTime::MakeDatetime(DateTime::StartOfWeek({col}))",
        TimeGrain.MONTH: "DateTime::MakeDatetime(DateTime::StartOfMonth({col}))",
        TimeGrain.QUARTER: ("DateTime::MakeDatetime(DateTime::StartOfQuarter({col}))"),
        TimeGrain.YEAR: "DateTime::MakeDatetime(DateTime::StartOfYear({col}))",
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

        if isinstance(sqla_type, types.Date):
            return (
                f"DateTime::MakeDate(DateTime::ParseIso8601("
                f"'{dttm.date().isoformat()}'))"
            )
        if isinstance(sqla_type, types.DateTime):
            return (
                f"DateTime::MakeDatetime(DateTime::ParseIso8601("
                f"""'{dttm.isoformat(sep="T", timespec="seconds")}'))"""
            )
        return None
