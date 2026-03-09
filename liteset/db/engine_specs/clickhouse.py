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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text

from liteset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec

_VALID_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.\-]*$")


class AsyncClickHouseEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for ClickHouse using asynch driver."""

    engine = "clickhouse"
    engine_name = "ClickHouse"
    default_driver = "asynch"

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
