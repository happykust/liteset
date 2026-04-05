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


class AsyncDatabendEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Databend."""

    engine = "databend"
    engine_name = "Databend"
    default_driver = "databend"

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "{col}"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": "DATE_TRUNC('SECOND', {col})",
        "PT1M": "to_start_of_minute(TO_DATETIME({col}))",
        "PT5M": "to_start_of_five_minutes(TO_DATETIME({col}))",
        "PT10M": "to_start_of_ten_minutes(TO_DATETIME({col}))",
        "PT15M": "to_start_of_fifteen_minutes(TO_DATETIME({col}))",
        "PT1H": "to_start_of_hour(TO_DATETIME({col}))",
        "P1D": "to_start_of_day(TO_DATETIME({col}))",
        "P1W": "to_monday(TO_DATETIME({col}))",
        "P1M": "to_start_of_month(TO_DATETIME({col}))",
        "P3M": "to_start_of_quarter(TO_DATETIME({col}))",
        "P1Y": "to_start_of_year(TO_DATETIME({col}))",
    }

    column_type_mappings: Any = (
        (
            re.compile(r".*Varchar.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*Array.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*Map.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*Json.*", re.IGNORECASE),
            types.JSON(),
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
            re.compile(r".*Float\d+.*", re.IGNORECASE),
            types.FLOAT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r".*Double\d+.*", re.IGNORECASE),
            types.FLOAT(),
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

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"to_date('{dttm.date().isoformat()}')"
        if isinstance(sqla_type, types.TIMESTAMP):
            return f"""TO_TIMESTAMP('{dttm.isoformat(timespec="microseconds")}')"""
        if isinstance(sqla_type, types.DateTime):
            return f"""to_dateTime('{dttm.isoformat(sep=" ", timespec="seconds")}')"""
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
