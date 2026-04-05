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


class AsyncFirebirdEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Firebird using firebirdsql driver."""

    engine = "firebird"
    engine_name = "Firebird"
    default_driver = "firebirdsql"

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "DATEADD(second, {col}, CAST('00:00:00' AS TIMESTAMP))"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": (
            "CAST(CAST({col} AS DATE) "
            "|| ' ' "
            "|| EXTRACT(HOUR FROM {col}) "
            "|| ':' "
            "|| EXTRACT(MINUTE FROM {col}) "
            "|| ':' "
            "|| FLOOR(EXTRACT(SECOND FROM {col})) AS TIMESTAMP)"
        ),
        "PT1M": (
            "CAST(CAST({col} AS DATE) "
            "|| ' ' "
            "|| EXTRACT(HOUR FROM {col}) "
            "|| ':' "
            "|| EXTRACT(MINUTE FROM {col}) "
            "|| ':00' AS TIMESTAMP)"
        ),
        "PT1H": (
            "CAST(CAST({col} AS DATE) "
            "|| ' ' "
            "|| EXTRACT(HOUR FROM {col}) "
            "|| ':00:00' AS TIMESTAMP)"
        ),
        "P1D": "CAST({col} AS DATE)",
        "P1M": (
            "CAST(EXTRACT(YEAR FROM {col}) "
            "|| '-' "
            "|| EXTRACT(MONTH FROM {col}) "
            "|| '-01' AS DATE)"
        ),
        "P1Y": "CAST(EXTRACT(YEAR FROM {col}) || '-01-01' AS DATE)",
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
            return f"CAST('{dttm.date().isoformat()}' AS DATE)"
        if isinstance(sqla_type, types.DateTime):
            dttm_formatted = dttm.isoformat(sep=" ")
            dttm_valid_precision = dttm_formatted[: len("YYYY-MM-DD HH:MM:SS.MMMM")]
            return f"CAST('{dttm_valid_precision}' AS TIMESTAMP)"
        if isinstance(sqla_type, types.Time):
            return f"CAST('{dttm.time().isoformat()}' AS TIME)"
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
