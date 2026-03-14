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
"""Native async engine spec for Trino using aiotrino driver."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from liteset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec


class AsyncTrinoEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Trino using aiotrino driver."""

    engine = "trino"
    engine_name = "Trino"
    default_driver = "aiotrino"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": "DATE_TRUNC('second', {col})",
        "PT1M": "DATE_TRUNC('minute', {col})",
        "PT1H": "DATE_TRUNC('hour', {col})",
        "P1D": "DATE_TRUNC('day', {col})",
        "P1W": "DATE_TRUNC('week', {col})",
        "P1M": "DATE_TRUNC('month', {col})",
        "P3M": "DATE_TRUNC('quarter', {col})",
        "P1Y": "DATE_TRUNC('year', {col})",
    }

    _custom_errors: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(
                r"line (?P<line>\d+):(?P<col>\d+): Column '(?P<column>.+?)' cannot be resolved"
            ),
            "Column '{column}' cannot be resolved (line {line}:{col})",
        ),
        (
            re.compile(r"Table '(?P<table>.+?)' does not exist"),
            "Table '{table}' does not exist",
        ),
        (
            re.compile(r"Schema '(?P<schema>.+?)' does not exist"),
            "Schema '{schema}' does not exist",
        ),
        (
            re.compile(r"Catalog '(?P<catalog>.+?)' does not exist"),
            "Catalog '{catalog}' does not exist",
        ),
        (
            re.compile(r"Access Denied"),
            "Access denied",
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
        args.setdefault("http_scheme", "https")
        return uri, args
