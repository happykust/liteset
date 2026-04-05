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

from sqlalchemy.types import Date, DateTime

from superset.db.engine_specs.postgres import AsyncPostgresEngineSpec


class AsyncDenodoEngineSpec(AsyncPostgresEngineSpec):
    """Async engine spec for Denodo (PostgreSQL wire protocol).

    Denodo uses a PostgreSQL-compatible interface but has its own
    SQL dialect with ``TRUNC``-based time grains and custom datetime
    formatting functions.
    """

    engine = "denodo"
    engine_name = "Denodo"
    default_driver = "asyncpg"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1M": "TRUNC({col},'MI')",
        "PT1H": "TRUNC({col},'HH')",
        "P1D": "TRUNC({col},'DDD')",
        "P1W": "TRUNC({col},'W')",
        "P1M": "TRUNC({col},'MONTH')",
        "P3M": "TRUNC({col},'Q')",
        "P1Y": "TRUNC({col},'YEAR')",
    }

    _custom_errors: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r"The username or password is incorrect"),
            "Incorrect username or password.",
        ),
        (
            re.compile(r"no password supplied"),
            "Please enter a password.",
        ),
        (
            re.compile(r'could not translate host name "(?P<hostname>.*?)" to address'),
            'Hostname "{hostname}" cannot be resolved.',
        ),
        (
            re.compile(r"Is the server running on that host and accepting"),
            "Server refused the connection: check hostname and port.",
        ),
        (
            re.compile(r"Database '(?P<database>.*?)' not found"),
            'Unable to connect to database "{database}".',
        ),
        (
            re.compile(
                r"Insufficient privileges to connect to the database "
                r"'(?P<database>.*?)'"
            ),
            'Unable to connect to database "{database}": insufficient permissions.',
        ),
        (
            re.compile(r"Exception parsing query near '(?P<err>.*?)'"),
            'Syntax error at or near "{err}".',
        ),
        (
            re.compile(r"Field not found '(?P<column>.*?)' in view '(?P<view>.*?)'"),
            'Column "{column}" not found in "{view}".',
        ),
    ]

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "GETTIMEFROMMILLIS({col})"

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)
        if isinstance(sqla_type, Date):
            return f"TO_DATE('yyyy-MM-dd', '{dttm.date().isoformat()}')"
        if isinstance(sqla_type, DateTime):
            dttm_formatted = dttm.isoformat(sep=" ", timespec="milliseconds")
            return f"TO_TIMESTAMP('yyyy-MM-dd HH:mm:ss.SSS', '{dttm_formatted}')"
        return None
