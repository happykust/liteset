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

from superset.db.engine_specs.postgres import AsyncPostgresEngineSpec


class AsyncCrateDbEngineSpec(AsyncPostgresEngineSpec):
    """Async engine spec for CrateDB (PostgreSQL wire protocol).

    CrateDB uses the PostgreSQL wire protocol but has its own SQL dialect
    with limited time-grain support and epoch-based timestamps.
    """

    engine = "crate"
    engine_name = "CrateDB"
    default_driver = "asyncpg"

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

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "{col} * 1000"

    @classmethod
    def epoch_ms_to_dttm(cls) -> str:
        # CrateDB stores ms-epoch natively, so ms → dttm is identity. Without
        # this override the base default emits ``({col}/1000) * 1000`` whose
        # integer division truncates sub-second precision.
        return "{col}"

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)
        if isinstance(sqla_type, types.TIMESTAMP):
            return f"CAST('{dttm.isoformat()}' AS TIMESTAMP)"
        return None
