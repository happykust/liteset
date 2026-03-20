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
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text

from liteset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec

_sync_db_pool = ThreadPoolExecutor(
    max_workers=int(os.environ.get("LITESET_SYNC_DB_POOL_SIZE", "16")),
    thread_name_prefix="sync-db",
)

logger = logging.getLogger(__name__)


def _is_overridden(cls: type, method_name: str) -> bool:
    """Check if a method is defined directly on cls (not just inherited)."""
    return method_name in cls.__dict__


class SyncFallbackEngineSpec(BaseAsyncEngineSpec):
    """Wraps a synchronous superset BaseEngineSpec via conn.run_sync().

    Used for database engines that don't have a native async driver.
    The sync execution runs in the greenlet context of the async
    connection (not a thread pool) via run_sync().

    Delegates inspect methods (get_catalog_names, get_schema_names, etc.)
    to the sync spec when it overrides them, falling back to the base
    information_schema queries otherwise.
    """

    _sync_spec: type  # Flask BaseEngineSpec subclass

    @classmethod
    async def get_catalog_names(
        cls,
        conn: AsyncConnection,
    ) -> set[str]:
        sync_spec = cls._sync_spec
        if _is_overridden(sync_spec, "get_catalog_names"):

            def _run(sync_conn: Connection) -> set[str]:
                inspector = inspect(sync_conn)
                try:
                    return set(sync_spec.get_catalog_names(inspector=inspector))
                except TypeError:
                    # Fallback: some specs expect database= instead of inspector=
                    return set(inspector.get_schema_names())

            return await conn.run_sync(_run)
        return await super().get_catalog_names(conn)

    @classmethod
    async def get_schema_names(
        cls,
        conn: AsyncConnection,
        catalog: str | None = None,
    ) -> set[str]:
        sync_spec = cls._sync_spec
        if _is_overridden(sync_spec, "get_schema_names"):

            def _run(sync_conn: Connection) -> set[str]:
                inspector = inspect(sync_conn)
                try:
                    return set(sync_spec.get_schema_names(inspector=inspector, catalog=catalog))
                except TypeError:
                    return set(inspector.get_schema_names())

            return await conn.run_sync(_run)
        return await super().get_schema_names(conn, catalog=catalog)

    @classmethod
    async def get_table_names(
        cls,
        conn: AsyncConnection,
        schema: str | None = None,
    ) -> set[str]:
        sync_spec = cls._sync_spec
        if _is_overridden(sync_spec, "get_table_names"):

            def _run(sync_conn: Connection) -> set[str]:
                inspector = inspect(sync_conn)
                try:
                    return set(sync_spec.get_table_names(inspector=inspector, schema=schema))
                except TypeError:
                    return set(inspector.get_table_names(schema=schema))

            return await conn.run_sync(_run)
        return await super().get_table_names(conn, schema=schema)

    @classmethod
    async def get_columns(
        cls,
        conn: AsyncConnection,
        table_name: str,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        sync_spec = cls._sync_spec
        if _is_overridden(sync_spec, "get_columns"):

            def _run(sync_conn: Connection) -> list[dict[str, Any]]:
                inspector = inspect(sync_conn)
                try:
                    return list(sync_spec.get_columns(inspector=inspector, table_name=table_name, schema=schema))
                except TypeError:
                    return [
                        {
                            "column_name": col["name"],
                            "data_type": str(col["type"]),
                            "is_nullable": col.get("nullable", True),
                        }
                        for col in inspector.get_columns(table_name, schema=schema)
                    ]

            return await conn.run_sync(_run)
        return await super().get_columns(conn, table_name=table_name, schema=schema)

    @classmethod
    async def execute(
        cls,
        conn: AsyncConnection,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> AsyncResultSet:
        def _run(sync_conn: Connection) -> AsyncResultSet:
            result = sync_conn.execute(text(query), parameters or {})
            columns = list(result.keys()) if result.returns_rows else []
            data = (
                [tuple(row) for row in result.fetchall()]
                if result.returns_rows
                else []
            )
            return AsyncResultSet(
                columns=columns,
                data=data,
                row_count=result.rowcount if result.rowcount >= 0 else len(data),
            )

        return await conn.run_sync(_run)

    @classmethod
    async def fetch_data(
        cls,
        conn: AsyncConnection,
        query: str,
        limit: int | None = None,
    ) -> list[tuple[Any, ...]]:
        def _run(sync_conn: Connection) -> list[tuple[Any, ...]]:
            result = sync_conn.execute(text(query))
            if limit is not None:
                return [tuple(row) for row in result.fetchmany(limit)]
            return [tuple(row) for row in result.fetchall()]

        return await conn.run_sync(_run)

    @classmethod
    def extract_errors(cls, ex: Exception) -> list[dict[str, Any]]:
        if _is_overridden(cls._sync_spec, "extract_errors"):
            try:
                sync_errors = cls._sync_spec.extract_errors(ex)
                return [
                    {
                        "message": str(getattr(e, "message", str(e))),
                        "error_type": getattr(e, "error_type", type(ex).__name__),
                    }
                    for e in sync_errors
                ]
            except Exception:
                logger.debug(
                    "Failed to extract errors via sync spec %s",
                    cls._sync_spec.__name__,
                    exc_info=True,
                )
        return super().extract_errors(ex)


def make_async_spec(
    sync_spec_cls: type,
) -> type[SyncFallbackEngineSpec]:
    """Dynamically create a SyncFallbackEngineSpec wrapping a sync spec class."""
    engine = getattr(sync_spec_cls, "engine", "") or ""
    engine_name = getattr(sync_spec_cls, "engine_name", "") or ""

    spec_cls = type(
        f"Async{sync_spec_cls.__name__}",
        (SyncFallbackEngineSpec,),
        {
            "_sync_spec": sync_spec_cls,
            "engine": engine,
            "engine_name": f"{engine_name} (sync fallback)",
            "default_driver": getattr(sync_spec_cls, "default_driver", "") or "",
            "_time_grain_expressions": dict(
                getattr(sync_spec_cls, "_time_grain_expressions", {})
            ),
        },
    )
    return spec_cls
