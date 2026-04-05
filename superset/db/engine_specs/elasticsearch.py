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
from datetime import datetime
from typing import Any

from packaging.version import Version
from sqlalchemy import types
from sqlalchemy.ext.asyncio import AsyncConnection

from superset.constants import TimeGrain
from superset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec

logger = logging.getLogger(__name__)


class AsyncElasticsearchEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Elasticsearch (SQL API).

    Ports time grain expressions and convert_dttm from the legacy
    ElasticSearchEngineSpec. Uses DATE_TRUNC for DATETIME columns.
    """

    engine = "elasticsearch"
    engine_name = "ElasticSearch (SQL API)"
    default_driver = "elasticsearch"

    _date_trunc_functions: dict[str, str] = {
        "DATETIME": "DATE_TRUNC",
    }

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        TimeGrain.SECOND: "{func}('second', {col})",
        TimeGrain.MINUTE: "{func}('minute', {col})",
        TimeGrain.HOUR: "{func}('hour', {col})",
        TimeGrain.DAY: "{func}('day', {col})",
        TimeGrain.WEEK: "{func}('week', {col})",
        TimeGrain.MONTH: "{func}('month', {col})",
        TimeGrain.YEAR: "{func}('year', {col})",
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
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        db_extra = db_extra or {}
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.DateTime):
            es_version = db_extra.get("version")
            # Elasticsearch 7.8+ supports DATETIME_PARSE which handles
            # time zones correctly, unlike CAST ... AS DATETIME.
            supports_dttm_parse = False
            try:
                if es_version:
                    supports_dttm_parse = Version(es_version) >= Version("7.8")
            except Exception:
                logger.error(
                    "Unexpected error while converting es_version",
                    exc_info=True,
                )

            if supports_dttm_parse:
                datetime_formatted = dttm.isoformat(sep=" ", timespec="seconds")
                return f"DATETIME_PARSE('{datetime_formatted}', 'yyyy-MM-dd HH:mm:ss')"

            return f"""CAST('{dttm.isoformat(timespec="seconds")}' AS DATETIME)"""

        return None


class AsyncOpenDistroEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for ElasticSearch (OpenDistro SQL).

    Ports time grain expressions and convert_dttm from the legacy
    OpenDistroEngineSpec.
    """

    engine = "odelasticsearch"
    engine_name = "ElasticSearch (OpenDistro SQL)"
    default_driver = "elasticsearch"

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        TimeGrain.SECOND: "date_format({col}, 'yyyy-MM-dd HH:mm:ss.000')",
        TimeGrain.MINUTE: "date_format({col}, 'yyyy-MM-dd HH:mm:00.000')",
        TimeGrain.HOUR: "date_format({col}, 'yyyy-MM-dd HH:00:00.000')",
        TimeGrain.DAY: "date_format({col}, 'yyyy-MM-dd 00:00:00.000')",
        TimeGrain.MONTH: "date_format({col}, 'yyyy-MM-01 00:00:00.000')",
        TimeGrain.YEAR: "date_format({col}, 'yyyy-01-01 00:00:00.000')",
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
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.DateTime):
            return f"""'{dttm.isoformat(timespec="seconds")}'"""
        return None
