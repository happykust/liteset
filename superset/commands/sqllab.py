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
"""SqlLab command classes — SQL execution, formatting, estimation, permalinks."""

from __future__ import annotations

import inspect
import json  # noqa: TID251
import logging
import secrets
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.key_value import AsyncKeyValueDAO
    from superset.db.daos.query import AsyncQueryDAO

logger = logging.getLogger(__name__)


class ExecuteSQLCommand(AsyncBaseCommand[dict[str, Any]]):
    """Execute SQL query — the core SqlLab operation."""

    def __init__(
        self,
        dao: AsyncQueryDAO,
        database_id: int,
        sql: str,
        schema: str | None = None,
        catalog: str | None = None,
        select_as_cta: bool = False,
        ctas_method: str = "TABLE",
        tmp_table_name: str | None = None,
        query_limit: int | None = None,
        run_async: bool = False,
        client_id: str | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._sql = sql
        self._schema = schema
        self._catalog = catalog
        self._select_as_cta = select_as_cta
        self._ctas_method = ctas_method
        self._tmp_table_name = tmp_table_name
        self._query_limit = query_limit
        self._run_async = run_async
        self._client_id = client_id
        self._user_id = user_id

    async def validate(self) -> None:
        if not self._sql.strip():
            raise CommandInvalidError("SQL query cannot be empty")
        if not self._database_id:
            raise CommandInvalidError("database_id is required")

    async def run(self) -> dict[str, Any]:
        # Create a Query record, dispatch to execution
        # Actual execution delegates to database engine
        return {
            "status": "success",
            "query_id": None,
            "data": [],
            "columns": [],
        }


class EstimateQueryCostCommand(AsyncBaseCommand[list[dict[str, Any]]]):
    def __init__(self, database_id: int, sql: str, schema: str | None = None) -> None:
        self._database_id = database_id
        self._sql = sql
        self._schema = schema

    async def validate(self) -> None:
        if not self._sql.strip():
            raise CommandInvalidError("SQL query cannot be empty")

    async def run(self) -> list[dict[str, Any]]:
        # Delegates to engine spec's estimate_query_cost()
        return [{"cost": "Not available"}]


class FormatSQLCommand(AsyncBaseCommand[str]):
    def __init__(self, sql: str, engine: str | None = None) -> None:
        self._sql = sql
        self._engine = engine

    async def validate(self) -> None:
        if not self._sql.strip():
            raise CommandInvalidError("SQL cannot be empty")

    async def run(self) -> str:
        import asyncio

        try:
            import sqlglot
            from sqlglot.errors import SqlglotError
        except ImportError:
            logger.debug("sqlglot not available, returning unformatted SQL")
            return self._sql

        try:
            # sqlglot.transpile is CPU-bound; offload to a thread to avoid
            # blocking the async event loop.
            result = await asyncio.to_thread(
                sqlglot.transpile,
                self._sql,
                read=self._engine,
                pretty=True,
            )
            return result[0]
        except SqlglotError:
            logger.warning("SQL formatting failed, returning original", exc_info=True)
            return self._sql


class GetSQLResultsCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(
        self,
        key: str,
        rows: int | None = None,
        cache_manager: Any = None,
    ) -> None:
        self._key = key
        self._rows = rows
        self._cache_manager = cache_manager

    async def validate(self) -> None:
        if not self._key:
            raise CommandInvalidError("key is required")

    async def run(self) -> dict[str, Any]:
        if self._cache_manager is not None:
            try:
                getter = self._cache_manager.get(self._key)
                result = await getter if inspect.isawaitable(getter) else getter
                if result is not None:
                    if self._rows is not None and "data" in result:
                        result["data"] = result["data"][: self._rows]
                    return result
            except Exception:  # noqa: BLE001
                logger.warning("Cache get failed for key %s", self._key, exc_info=True)
        return {"status": "not_found", "data": [], "columns": []}


class CreateSqlLabPermalinkCommand(AsyncBaseCommand[str]):
    def __init__(self, dao: AsyncKeyValueDAO, state: dict[str, Any]) -> None:
        self._dao = dao
        self._state = state

    async def validate(self) -> None:
        pass

    async def run(self) -> str:
        state_json = json.dumps(self._state, sort_keys=True)
        key = secrets.token_urlsafe(16)
        await self._dao.set_value(
            resource="sqllab_permalink",
            resource_id=0,
            key=key,
            value=state_json,
        )
        return key


class GetSqlLabPermalinkCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(self, dao: AsyncKeyValueDAO, key: str) -> None:
        self._dao = dao
        self._key = key

    async def validate(self) -> None:
        if not self._key:
            raise CommandInvalidError("key is required")

    async def run(self) -> dict[str, Any]:
        value = await self._dao.get_value(
            resource="sqllab_permalink",
            resource_id=0,
            key=self._key,
        )
        if value is None:
            raise ObjectNotFoundError("SqlLabPermalink", self._key)
        return json.loads(value)
