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

import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import types
from sqlalchemy.dialects.mysql import (
    BIT,
    DECIMAL,
    DOUBLE,
    FLOAT,
    INTEGER,
    LONGTEXT,
    MEDIUMINT,
    MEDIUMTEXT,
    TINYINT,
    TINYTEXT,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text

from superset.db.engine_specs.base import (
    AsyncResultSet,
    BaseAsyncEngineSpec,
    ColumnTypeMapping,
)
from superset.typing import GenericDataType

logger = logging.getLogger(__name__)


class AsyncMySQLEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for MySQL using asyncmy driver."""

    engine = "mysql"
    engine_name = "MySQL"
    default_driver = "asyncmy"
    max_column_name_length = 64

    supports_dynamic_schema: bool = True

    column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r"^int.*", re.IGNORECASE),
            INTEGER(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^tinyint", re.IGNORECASE),
            TINYINT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^mediumint", re.IGNORECASE),
            MEDIUMINT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^decimal", re.IGNORECASE),
            DECIMAL(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^float", re.IGNORECASE),
            FLOAT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^double", re.IGNORECASE),
            DOUBLE(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bit", re.IGNORECASE),
            BIT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^tinytext", re.IGNORECASE),
            TINYTEXT(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^mediumtext", re.IGNORECASE),
            MEDIUMTEXT(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^longtext", re.IGNORECASE),
            LONGTEXT(),
            GenericDataType.STRING,
        ),
    )

    column_type_mutators: dict[type[types.TypeEngine[Any]], Callable[[Any], Any]] = {
        DECIMAL: lambda val: Decimal(val) if isinstance(val, str) else val,
    }

    disallow_uri_query_params: dict[str, set[str]] = {
        "mysqldb": {"local_infile"},
        "mysqlconnector": {"allow_local_infile"},
        "asyncmy": {"local_infile"},
    }
    enforce_uri_query_params: dict[str, dict[str, int]] = {
        "mysqldb": {"local_infile": 0},
        "mysqlconnector": {"allow_local_infile": 0},
        "asyncmy": {"local_infile": 0},
    }

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
        (
            re.compile(
                # NOTE: no closing quote after the capture group.
                r"check the manual that corresponds to your MySQL server "
                r"version for the right syntax to use near '(?P<server_error>.*)"
            ),
            'Please check your query for syntax errors near "{server_error}". '
            "Then, try running your query again.",
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
    def epoch_to_dttm(cls) -> str:
        return "from_unixtime({col})"

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"STR_TO_DATE('{dttm.date().isoformat()}', '%Y-%m-%d')"
        if isinstance(sqla_type, types.DateTime):
            datetime_formatted = dttm.isoformat(sep=" ", timespec="microseconds")
            return f"STR_TO_DATE('{datetime_formatted}', '%Y-%m-%d %H:%i:%s.%f')"
        return None

    @classmethod
    async def get_cancel_query_id(
        cls,
        conn: AsyncConnection,
    ) -> str | None:
        result = await conn.execute(text("SELECT CONNECTION_ID()"))
        row = result.fetchone()
        if row:
            return str(row[0])
        return None

    @classmethod
    async def cancel_query(
        cls,
        conn: AsyncConnection,
        cancel_query_id: str,
    ) -> bool:
        try:
            await conn.execute(text(f"KILL CONNECTION {cancel_query_id}"))
        except Exception:
            logger.exception(
                "Failed to cancel MySQL query (connection_id=%s)",
                cancel_query_id,
            )
            return False
        return True

    @classmethod
    def adjust_engine_params(
        cls,
        uri: str,
        connect_args: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        # Call super() first so enforce_uri_query_params (including
        # {"asyncmy": {"local_infile": 0}}) is merged.
        uri, args = super().adjust_engine_params(uri, connect_args)
        args.setdefault("charset", "utf8mb4")
        args.setdefault("connect_timeout", 10)
        return uri, args
