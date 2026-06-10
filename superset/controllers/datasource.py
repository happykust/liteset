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
"""Datasource controller — get datasource by type and ID."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from litestar import Controller, get, Response
from litestar.di import Provide

from superset.events import event_logger
from superset.exceptions import SupersetSecurityException
from superset.guards.rbac import require_permission
from superset.providers import provide_datasource_dao
from superset.typing import (
    DatasourceDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)
from superset.utils.core import DatasourceType

logger = logging.getLogger(__name__)


class DatasourceController(Controller):
    """Datasource API endpoints.

    Provides access to datasources by type and ID, mirroring the Flask
    ``DatasourceRestApi`` and ``Datasource`` view endpoints.
    """

    path = "/api/v1/datasource"
    tags = ["Datasources"]
    dependencies = {
        "ds_dao": Provide(provide_datasource_dao, sync_to_thread=False),
    }

    @get(
        "/{datasource_type:str}/{datasource_id:int}",
        # Original DatasourceRestApi has ``class_permission_name = "Datasource"``
        # (superset_old/datasource/api.py:35) — permissions are registered
        # under the "Datasource" view menu, not the class name.
        guards=[require_permission("can_get", "Datasource")],
    )
    async def get_datasource(
        self,
        datasource_type: str,
        datasource_id: int,
        ds_dao: DatasourceDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any] | Response[Any]:
        """GET /api/v1/datasource/{datasource_type}/{datasource_id} — get a datasource.

        Retrieves datasource metadata by type and ID. Accepts all
        ``DatasourceType`` enum values (table, query, saved_query, etc.)
        and delegates to the DAO.

        Returns the datasource attributes as a dict.
        """
        # Validate datasource_type via DatasourceType enum coercion — 1:1 with
        # original ``DatasourceType(datasource_type)`` which raises ValueError
        # for unknown types.
        try:
            DatasourceType(datasource_type)
        except ValueError:
            return Response(
                content={"message": f"Invalid datasource type: {datasource_type}"},
                status_code=400,
            )

        try:
            datasource = await ds_dao.get_datasource(datasource_type, datasource_id)
        except ValueError:
            return Response(
                content={
                    "message": "DAO datasource query source type is not supported"
                },
                status_code=400,
            )

        if datasource is None:
            return Response(
                content={"message": "Datasource does not exist"},
                status_code=404,
            )

        # Enforce datasource access — this endpoint exposes schema, columns and
        # (for virtual datasets) the SQL; without the check any authenticated
        # user (e.g. Gamma) could read it, bypassing the dataset access control
        # (``GET /dataset/{id}`` correctly 404s). Mirrors ``get_column_values``
        # / upstream ``datasource.raise_for_access()``.
        if hasattr(security_manager, "raise_for_access"):
            try:
                await self._call_raise_for_access(
                    security_manager, datasource, datasource_type, current_user
                )
            except SupersetSecurityException as ex:
                return Response(
                    content={"message": getattr(ex, "message", str(ex))},
                    status_code=403,
                )

        # Build response from datasource attributes
        result: dict[str, Any] = {
            "id": getattr(datasource, "id", None),
            "type": datasource_type,
            "datasource_name": getattr(datasource, "datasource_name", None)
            or getattr(datasource, "table_name", None),
            "schema": getattr(datasource, "schema", None),
            "database_id": getattr(datasource, "database_id", None),
            "database_name": None,
            "description": getattr(datasource, "description", None),
            "sql": getattr(datasource, "sql", None),
        }

        # Include database name if the relationship is loaded
        database = getattr(datasource, "database", None)
        if database is not None:
            result["database_name"] = getattr(database, "database_name", None)

        # Include column names if loaded
        columns = getattr(datasource, "columns", None)
        if columns is not None:
            result["columns"] = [
                {
                    "id": getattr(col, "id", None),
                    "column_name": getattr(col, "column_name", None),
                    "type": getattr(col, "type", None),
                    "is_dttm": getattr(col, "is_dttm", False),
                    "filterable": getattr(col, "filterable", True),
                    "groupby": getattr(col, "groupby", True),
                }
                for col in columns
            ]

        await event_logger.alog_with_context(
            "datasource.get",
            extra={"datasource_type": datasource_type, "datasource_id": datasource_id},
        )
        return {"result": result}

    @staticmethod
    async def _call_raise_for_access(
        security_manager: SecurityManagerProtocol,
        datasource: Any,
        datasource_type: str,
        current_user: UserProtocol,
    ) -> None:
        """Dispatch raise_for_access via the correct path for the datasource type.

        Mirrors the original ``datasource.raise_for_access()`` dispatch:

        * ``Query.raise_for_access()`` calls
          ``security_manager.raise_for_access(query=self)``
          → Path 1 (database + per-table permission check).
        * ``SqlaTable.raise_for_access()`` calls
          ``security_manager.raise_for_access(datasource=self)``
          → Path 3 (datasource / schema access check).

        Passing a Query as ``datasource=`` (Path 3) would evaluate
        ``Query.perm`` (``"[db].[tab](id:N)"``) as a datasource_access
        permission string — a format never registered in FAB — and would
        grant access only to admins and owners, denying users who have only
        table-level permissions.  The original correctly routes through
        Path 1, which parses the SQL and checks per-table grants.
        """
        if datasource_type == DatasourceType.QUERY:
            await security_manager.raise_for_access(query=datasource, user=current_user)
        elif str(datasource_type).lower() == "saved_query":
            raise AttributeError(
                f"'{type(datasource).__name__}' object has no attribute "
                "'raise_for_access'"
            )
        else:
            await security_manager.raise_for_access(
                datasource=datasource, user=current_user
            )

    @staticmethod
    async def _fetch_rls_clauses(
        security_manager: SecurityManagerProtocol,
        datasource: Any,
        current_user: UserProtocol,
    ) -> list[Any]:
        """Return active RLS filter clauses for ``datasource``.

        Delegates to :func:`superset.utils.rls.compose_rls_where_clauses`
        which preserves the original ``group_key`` OR/AND grouping and
        Jinja templating semantics, returning a list of SQLAlchemy
        ``ClauseElement`` objects (``TextClause`` / ``BooleanClauseList``).
        Downstream callers (e.g. ``async_values_for_column``) compile
        these against the database dialect for proper quoting.
        """
        from superset.utils.rls import compose_rls_where_clauses

        if not hasattr(security_manager, "get_rls_filters"):
            return []
        return await compose_rls_where_clauses(
            datasource,
            user=current_user,
            security_manager=security_manager,
        )

    @staticmethod
    def _resolve_row_limit() -> int:
        """Return the effective row limit for filter column value queries.

        Mirrors upstream ``apply_max_row_limit(FILTER_SELECT_ROW_LIMIT)``:
        ``min(SQL_MAX_ROW, FILTER_SELECT_ROW_LIMIT)``. Falls back to 10 000
        if settings cannot be loaded.
        """
        try:
            from superset import config as _config

            _settings = _config.SupersetSettings()
            _fsrl = int(getattr(_settings, "filter_select_row_limit", 10000))
            _smr = int(getattr(_settings, "sql_max_row", 100000))
            return min(_smr, _fsrl) if _fsrl else _smr
        except Exception:  # noqa: BLE001
            logger.debug("Could not load settings for row limit", exc_info=True)
            return 10000

    async def _invoke_async_values(
        self,
        datasource: Any,
        datasource_type: str,
        datasource_id: int,
        column_name: str,
        rls_clauses: list[Any],
    ) -> dict[str, Any] | Response[Any]:
        """Call ``datasource.async_values_for_column`` and return the result.

        Handles ``KeyError`` (unknown column → 400) and ``NotImplementedError``
        (unsupported datasource type → 400) exactly as the original does.
        """
        row_limit = self._resolve_row_limit()
        try:
            payload = await datasource.async_values_for_column(
                column_name=column_name,
                limit=row_limit,
                rls_filters=rls_clauses or None,
                # 1:1 upstream datasource/api.py: denormalize unless the
                # dataset is configured with normalized columns.
                denormalize_column=not getattr(datasource, "normalize_columns", False),
            )
            await event_logger.alog_with_context(
                "datasource.column_values",
                extra={
                    "datasource_type": datasource_type,
                    "datasource_id": datasource_id,
                    "column_name": column_name,
                },
            )
            return {"result": payload}
        except KeyError:
            return Response(
                content={"message": f"Column name {column_name} does not exist"},
                status_code=400,
            )
        except NotImplementedError:
            return Response(
                content={
                    "message": (
                        "Unable to get column values for "
                        f"datasource type: {datasource_type}"
                    )
                },
                status_code=400,
            )

    async def _fallback_column_values(
        self,
        datasource: Any,
        datasource_type: str,
        datasource_id: int,
        column_name: str,
    ) -> dict[str, Any] | Response[Any]:
        """Fallback when ``async_values_for_column`` is not available.

        For datasources that inherit the sync ``values_for_column`` (e.g.
        ``Query`` via ``ExploreMixin``), executes it in a worker thread to
        return real distinct column values — 1:1 with the original
        ``DatasourceRestApi.get_column_values`` which calls
        ``datasource.values_for_column(...)`` unconditionally for all
        datasource types including ``query``.

        Falls back to an empty list only when neither
        ``async_values_for_column`` nor ``values_for_column`` is present
        (truly unsupported type).
        """
        row_limit = self._resolve_row_limit()
        denormalize_column = not getattr(datasource, "normalize_columns", False)

        # Prefer the sync values_for_column inherited from ExploreMixin (e.g.
        # Query) — run it in a thread to avoid blocking the event loop.
        if hasattr(datasource, "values_for_column"):
            try:
                payload = await asyncio.to_thread(
                    datasource.values_for_column,
                    column_name=column_name,
                    limit=row_limit,
                    denormalize_column=denormalize_column,
                )
            except KeyError:
                return Response(
                    content={"message": f"Column name {column_name} does not exist"},
                    status_code=400,
                )
            except NotImplementedError:
                return Response(
                    content={
                        "message": (
                            "Unable to get column values for "
                            f"datasource type: {datasource_type}"
                        )
                    },
                    status_code=400,
                )
            await event_logger.alog_with_context(
                "datasource.column_values",
                extra={
                    "datasource_type": datasource_type,
                    "datasource_id": datasource_id,
                    "column_name": column_name,
                },
            )
            return {"result": payload}

        # Truly unsupported datasource type: check column exists then return [].
        columns = getattr(datasource, "columns", None)
        if columns is not None:
            col_names = [getattr(c, "column_name", None) for c in columns]
            if column_name not in col_names:
                return Response(
                    content={"message": f"Column name {column_name} does not exist"},
                    status_code=400,
                )

        await event_logger.alog_with_context(
            "datasource.column_values",
            extra={
                "datasource_type": datasource_type,
                "datasource_id": datasource_id,
                "column_name": column_name,
            },
        )
        return {"result": []}

    @get(
        "/{datasource_type:str}/{datasource_id:int}/column/{column_name:str}/values/",
        # Original ``@protect()`` generates ("can_get_column_values",
        # "Datasource") (superset_old/datasource/api.py:43 +
        # class_permission_name); the perm is NOT in READ_ONLY_PERMISSION,
        # so plain Gamma does not receive it.
        guards=[require_permission("can_get_column_values", "Datasource")],
    )
    async def get_column_values(
        self,
        datasource_type: str,
        datasource_id: int,
        column_name: str,
        ds_dao: DatasourceDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any] | Response[Any]:
        """GET /api/v1/datasource/{type}/{id}/column/{name}/values/

        Returns distinct values for a datasource column (used for filter UIs).
        """
        # Validate datasource_type via DatasourceType enum coercion — 1:1 with
        # original ``DatasourceType(datasource_type)`` (returns 400 on ValueError).
        try:
            DatasourceType(datasource_type)
        except ValueError:
            return Response(
                content={"message": f"Invalid datasource type: {datasource_type}"},
                status_code=400,
            )

        try:
            datasource = await ds_dao.get_datasource(datasource_type, datasource_id)
        except ValueError:
            return Response(
                content={
                    "message": "DAO datasource query source type is not supported"
                },
                status_code=400,
            )

        if datasource is None:
            return Response(
                content={"message": "Datasource does not exist"},
                status_code=404,
            )

        # Enforce datasource-level access BEFORE reading any values — 1:1 with
        # upstream ``datasource/api.py::get_column_values`` which calls
        # ``datasource.raise_for_access()`` first. Without it, any authenticated
        # user (e.g. Gamma with no datasource access) could read distinct column
        # values of ANY datasource → data leak.
        if hasattr(security_manager, "raise_for_access"):
            try:
                await self._call_raise_for_access(
                    security_manager, datasource, datasource_type, current_user
                )
            except SupersetSecurityException as ex:
                return Response(
                    content={"message": getattr(ex, "message", str(ex))},
                    status_code=403,
                )

        # Expose the current user to the Jinja template processor context
        # var so macros like ``{{ current_username() }}`` — commonly used
        # inside ``fetch_values_predicate`` — resolve correctly when
        # ``SqlaTable.async_values_for_column`` renders the predicate.
        from superset.utils.core import set_current_user

        set_current_user(current_user)

        # Gather Row-Level Security filter clauses for this datasource.
        # Mirrors ``query_context_processor._get_query_result`` and matches
        # the original sync ``values_for_column`` which calls
        # ``self.get_sqla_row_level_filters`` internally.
        rls_clauses = await self._fetch_rls_clauses(
            security_manager, datasource, current_user
        )

        # Use the async port of ``values_for_column``. The original sync
        # implementation in ``helpers.py`` requires a sync SQLAlchemy
        # engine that we don't wire up in the Litestar port; instead we
        # provide ``SqlaTable.async_values_for_column`` which runs
        # against the existing asyncpg connection pool.
        if hasattr(datasource, "async_values_for_column"):
            return await self._invoke_async_values(
                datasource=datasource,
                datasource_type=datasource_type,
                datasource_id=datasource_id,
                column_name=column_name,
                rls_clauses=rls_clauses,
            )

        # Fallback: check if column exists and return empty
        return await self._fallback_column_values(
            datasource=datasource,
            datasource_type=datasource_type,
            datasource_id=datasource_id,
            column_name=column_name,
        )
