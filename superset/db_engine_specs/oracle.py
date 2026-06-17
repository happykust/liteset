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
"""Oracle database engine spec."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import types

from superset.constants import TimeGrain
from superset.db_engine_specs.base import BaseEngineSpec


class OracleEngineSpec(BaseEngineSpec):
    engine = "oracle"
    engine_name = "Oracle"
    force_column_alias_quotes = True
    max_column_name_length = 128

    _time_grain_expressions = {
        None: "{col}",
        TimeGrain.SECOND: "CAST({col} as DATE)",
        TimeGrain.MINUTE: "TRUNC(CAST({col} as DATE), 'MI')",
        TimeGrain.HOUR: "TRUNC(CAST({col} as DATE), 'HH')",
        TimeGrain.DAY: "TRUNC(CAST({col} as DATE), 'DDD')",
        TimeGrain.WEEK: "TRUNC(CAST({col} as DATE), 'WW')",
        TimeGrain.MONTH: "TRUNC(CAST({col} as DATE), 'MONTH')",
        TimeGrain.QUARTER: "TRUNC(CAST({col} as DATE), 'Q')",
        TimeGrain.YEAR: "TRUNC(CAST({col} as DATE), 'YEAR')",
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
    def epoch_to_dttm(cls) -> str:
        return "TO_DATE('1970-01-01','YYYY-MM-DD')+(1/24/60/60)*{col}"

    @classmethod
    def epoch_ms_to_dttm(cls) -> str:
        return "TO_DATE('1970-01-01','YYYY-MM-DD')+(1/24/60/60/1000)*{col}"

    @classmethod
    def fetch_data(cls, cursor: Any, limit: int | None = None) -> list[tuple[Any, ...]]:
        if not cursor.description:
            return []
        return super().fetch_data(cursor, limit)


__all__ = [
    "OracleEngineSpec",
]
