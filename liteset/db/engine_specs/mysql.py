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

from liteset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec


class AsyncMySQLEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for MySQL using asyncmy driver."""

    engine = "mysql"
    engine_name = "MySQL"
    default_driver = "asyncmy"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": (
            "DATE_ADD(DATE({col}), "
            "INTERVAL (HOUR({col})*60*60 + MINUTE({col})*60 + SECOND({col})) SECOND)"
        ),
        "PT1M": (
            "DATE_ADD(DATE({col}), INTERVAL (HOUR({col})*60 + MINUTE({col})) MINUTE)"
        ),
        "PT1H": "DATE_ADD(DATE({col}), INTERVAL HOUR({col}) HOUR)",
        "P1D": "DATE({col})",
        "P1W": "DATE(DATE_SUB({col}, INTERVAL DAYOFWEEK({col}) - 1 DAY))",
        "1969-12-29T00:00:00Z/P1W": (
            "DATE(DATE_SUB({col}, "
            "INTERVAL DAYOFWEEK(DATE_SUB({col}, INTERVAL 1 DAY)) - 1 DAY))"
        ),
        "P1M": "DATE(DATE_SUB({col}, INTERVAL DAYOFMONTH({col}) - 1 DAY))",
        "P3M": (
            "MAKEDATE(YEAR({col}), 1) + "
            "INTERVAL QUARTER({col}) QUARTER - INTERVAL 1 QUARTER"
        ),
        "P1Y": "DATE(DATE_SUB({col}, INTERVAL DAYOFYEAR({col}) - 1 DAY))",
    }

    _custom_errors: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r"Access denied for user '(?P<username>.*?)'"),
            "Access denied for user: {username}",
        ),
        (
            re.compile(r"Unknown MySQL server host '(?P<host>.*?)'"),
            "Unknown hostname: {host}",
        ),
        (
            re.compile(r"Can't connect to MySQL server on '(?P<host>.*?)'"),
            "Cannot connect to MySQL server: {host}",
        ),
        (
            re.compile(r"Unknown database '(?P<database>.*?)'"),
            "Unknown database: {database}",
        ),
    ]

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
    def adjust_engine_params(
        cls,
        uri: str,
        connect_args: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        args = connect_args.copy() if connect_args else {}
        args.setdefault("charset", "utf8mb4")
        args.setdefault("connect_timeout", 10)
        return uri, args
