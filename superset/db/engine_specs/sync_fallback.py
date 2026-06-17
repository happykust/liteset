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
from collections.abc import Callable
from typing import Any

from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text

from superset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec

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

    _sync_spec: Any  # synchronous BaseEngineSpec subclass

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
                    # Sync spec has incompatible signature — re-raise rather
                    # than silently returning schema names as catalog names.
                    raise

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
                    return set(
                        sync_spec.get_schema_names(
                            inspector=inspector,
                            catalog=catalog,
                        )
                    )
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
                    return set(
                        sync_spec.get_table_names(
                            inspector=inspector,
                            schema=schema,
                        )
                    )
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
                    return list(
                        sync_spec.get_columns(
                            inspector=inspector,
                            table_name=table_name,
                            schema=schema,
                        )
                    )
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
                [tuple(row) for row in result.fetchall()] if result.returns_rows else []
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
            # Capture cursor.description before fetchall() because SA 2.0
            # calls _soft_close() after fetchall which sets result.cursor=None.
            # fetchmany() does not soft-close on a partial fetch, so it is safe
            # either way, but capturing early is harmless for both paths.
            description = result.cursor.description if result.cursor else None
            if limit is not None:
                data = [tuple(row) for row in result.fetchmany(limit)]
            else:
                data = [tuple(row) for row in result.fetchall()]

            if cls.column_type_mutators and data:
                description = description or []
                column_mutators: dict[int, Callable[[Any], Any]] = {}
                for idx, row in enumerate(description):
                    type_code = row[1]
                    datatype = cls.get_datatype(type_code)
                    sqla_type = cls.get_sqla_column_type(datatype)
                    if sqla_type is not None:
                        func = cls.column_type_mutators.get(
                            type(sqla_type)  # type: ignore[arg-type]
                        )
                        if func is not None:
                            column_mutators[idx] = func

                if column_mutators:
                    for row_idx, row_data in enumerate(data):
                        new_row = list(row_data)
                        for col_idx, func in column_mutators.items():
                            new_row[col_idx] = func(row_data[col_idx])
                        data[row_idx] = tuple(new_row)

            return data

        return await conn.run_sync(_run)

    @classmethod
    def extract_errors(cls, ex: Exception) -> list[dict[str, Any]]:
        # Always delegate to the sync spec's extract_errors first.
        # Even when the sync spec does not override extract_errors itself,
        # the base BaseEngineSpec.extract_errors uses cls.custom_errors
        # which may be populated on the sync spec (e.g. BigQuery, Snowflake).
        # Using _is_overridden would miss those engines.
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

    @classmethod
    async def get_view_names(
        cls,
        conn: AsyncConnection,
        schema: str | None = None,
    ) -> set[str]:
        sync_spec = cls._sync_spec
        if _is_overridden(sync_spec, "get_view_names"):

            def _run(sync_conn: Connection) -> set[str]:
                inspector = inspect(sync_conn)
                try:
                    return set(
                        sync_spec.get_view_names(
                            inspector=inspector,
                            schema=schema,
                        )
                    )
                except TypeError:
                    return set(inspector.get_view_names(schema=schema))

            return await conn.run_sync(_run)
        return await super().get_view_names(conn, schema=schema)

    @classmethod
    def get_datatype(cls, type_code: Any) -> str | None:
        """Delegate to the wrapped sync spec's get_datatype.

        Engine-specific DBAPI drivers (e.g. MySQLdb) return integer OID codes
        in cursor.description, not strings.  The sync spec's get_datatype maps
        those integers to type-name strings so that column_type_mutators can be
        applied.  Without this delegation the mutators would never fire because
        BaseAsyncEngineSpec.get_datatype returns None for non-string codes.
        """
        sync_spec = getattr(cls, "_sync_spec", None)
        if sync_spec is not None:
            return sync_spec.get_datatype(type_code)
        return super().get_datatype(type_code)

    @classmethod
    def convert_dttm(
        cls, target_type: str, dttm: Any, db_extra: dict[str, Any] | None = None
    ) -> str | None:
        sync_spec = cls._sync_spec
        if hasattr(sync_spec, "convert_dttm"):
            try:
                return sync_spec.convert_dttm(target_type, dttm, db_extra=db_extra)
            except TypeError:
                try:
                    return sync_spec.convert_dttm(target_type, dttm)
                except Exception:  # noqa: S110
                    pass
        return None


def make_async_spec(
    sync_spec_cls: type,
) -> type[SyncFallbackEngineSpec]:
    engine = getattr(sync_spec_cls, "engine", "") or ""
    engine_name = getattr(sync_spec_cls, "engine_name", "") or ""

    attrs: dict[str, Any] = {
        "_sync_spec": sync_spec_cls,
        "engine": engine,
        "engine_name": f"{engine_name} (sync fallback)",
        "default_driver": getattr(sync_spec_cls, "default_driver", "") or "",
        "_time_grain_expressions": dict(
            getattr(sync_spec_cls, "_time_grain_expressions", {})
        ),
    }

    for attr in (
        "epoch_to_dttm",
        "column_type_mappings",
        "column_type_mutators",
        "enforce_uri_query_params",
        "supports_dynamic_schema",
        "supports_catalog",
        "get_allow_cost_estimate",
        "time_groupby_inline",
        "try_remove_schema_from_table_name",
        "allow_dml",
        "allows_subqueries",
        "allows_alias_in_select",
        "allows_alias_in_orderby",
        "force_column_alias_quotes",
    ):
        val = getattr(sync_spec_cls, attr, None)
        if val is not None:
            attrs[attr] = val

    spec_cls = type(
        f"Async{sync_spec_cls.__name__}",
        (SyncFallbackEngineSpec,),
        attrs,
    )
    return spec_cls
