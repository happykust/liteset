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


class AsyncPostgresEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for PostgreSQL using asyncpg driver."""

    engine = "postgresql"
    engine_name = "PostgreSQL"
    default_driver = "asyncpg"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": "DATE_TRUNC('second', {col})",
        "PT5S": (
            "DATE_TRUNC('minute', {col}) + INTERVAL '5 seconds' * "
            "FLOOR(EXTRACT(SECOND FROM {col}) / 5)"
        ),
        "PT30S": (
            "DATE_TRUNC('minute', {col}) + INTERVAL '30 seconds' * "
            "FLOOR(EXTRACT(SECOND FROM {col}) / 30)"
        ),
        "PT1M": "DATE_TRUNC('minute', {col})",
        "PT5M": (
            "DATE_TRUNC('hour', {col}) + INTERVAL '5 minutes' * "
            "FLOOR(EXTRACT(MINUTE FROM {col}) / 5)"
        ),
        "PT10M": (
            "DATE_TRUNC('hour', {col}) + INTERVAL '10 minutes' * "
            "FLOOR(EXTRACT(MINUTE FROM {col}) / 10)"
        ),
        "PT15M": (
            "DATE_TRUNC('hour', {col}) + INTERVAL '15 minutes' * "
            "FLOOR(EXTRACT(MINUTE FROM {col}) / 15)"
        ),
        "PT30M": (
            "DATE_TRUNC('hour', {col}) + INTERVAL '30 minutes' * "
            "FLOOR(EXTRACT(MINUTE FROM {col}) / 30)"
        ),
        "PT1H": "DATE_TRUNC('hour', {col})",
        "P1D": "DATE_TRUNC('day', {col})",
        "P1W": "DATE_TRUNC('week', {col})",
        "P1M": "DATE_TRUNC('month', {col})",
        "P3M": "DATE_TRUNC('quarter', {col})",
        "P1Y": "DATE_TRUNC('year', {col})",
    }

    _custom_errors: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r'role "(?P<username>.*?)" does not exist'),
            "Invalid username: {username}",
        ),
        (
            re.compile(r"password authentication failed for user"),
            "Invalid password",
        ),
        (
            re.compile(r'could not translate host name "(?P<host>.*?)"'),
            "Invalid hostname: {host}",
        ),
        (
            re.compile(r"could not connect to server.*Connection refused"),
            "Port is closed or host is unreachable",
        ),
        (
            re.compile(r'database "(?P<database>.*?)" does not exist'),
            "Unknown database: {database}",
        ),
        (
            re.compile(r'column "(?P<column>.*?)" does not exist'),
            "Column does not exist: {column}",
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
        args.setdefault("statement_cache_size", 0)
        args.setdefault("prepared_statement_cache_size", 0)
        return uri, args
