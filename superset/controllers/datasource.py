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

import logging
from typing import Any

from litestar import Controller, Response, get
from litestar.di import Provide

from superset.events import event_logger
from superset.exceptions import (
    ObjectNotFoundError,
    SupersetSecurityException,
    SupersetValidationException,
)
from superset.guards.rbac import require_authenticated_user, require_authentication
from superset.providers import provide_datasource_dao
from superset.typing import (
    DatasourceDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)

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
        guards=[require_authentication],
    )
    async def get_datasource(
        self,
        datasource_type: str,
        datasource_id: int,
        ds_dao: DatasourceDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/datasource/{datasource_type}/{datasource_id} — get a datasource.

        Retrieves datasource metadata by type and ID. Supported types:
        ``table`` (SqlaTable).

        Returns the datasource attributes as a dict.
        """
        # Validate datasource_type
        allowed_types = ("table",)
        if datasource_type not in allowed_types:
            raise SupersetValidationException(
                f"Invalid datasource type: {datasource_type}. "
                f"Supported types: {', '.join(allowed_types)}"
            )

        try:
            datasource = await ds_dao.get_datasource(datasource_type, datasource_id)
        except ValueError as exc:
            raise SupersetValidationException(str(exc)) from exc

        if datasource is None:
            raise ObjectNotFoundError("Datasource", datasource_id)

        # Enforce datasource access — this endpoint exposes schema, columns and
        # (for virtual datasets) the SQL; without the check any authenticated
        # user (e.g. Gamma) could read it, bypassing the dataset access control
        # (``GET /dataset/{id}`` correctly 404s). Mirrors ``get_column_values``
        # / upstream ``datasource.raise_for_access()``.
        if hasattr(security_manager, "raise_for_access"):
            try:
                await security_manager.raise_for_access(
                    datasource=datasource, user=current_user
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

    @get(
        "/{datasource_type:str}/{datasource_id:int}/column/{column_name:str}/values/",
        guards=[require_authenticated_user],
    )
    async def get_column_values(
        self,
        datasource_type: str,
        datasource_id: int,
        column_name: str,
        ds_dao: DatasourceDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/datasource/{type}/{id}/column/{name}/values/

        Returns distinct values for a datasource column (used for filter UIs).
        """
        allowed_types = ("table",)
        if datasource_type not in allowed_types:
            raise SupersetValidationException(
                f"Invalid datasource type: {datasource_type}. "
                f"Supported types: {', '.join(allowed_types)}"
            )

        try:
            datasource = await ds_dao.get_datasource(datasource_type, datasource_id)
        except ValueError as exc:
            raise SupersetValidationException(str(exc)) from exc

        if datasource is None:
            raise ObjectNotFoundError("Datasource", datasource_id)

        # Enforce datasource-level access BEFORE reading any values — 1:1 with
        # upstream ``datasource/api.py::get_column_values`` which calls
        # ``datasource.raise_for_access()`` first. Without it, any authenticated
        # user (e.g. Gamma with no datasource access) could read distinct column
        # values of ANY datasource → data leak.
        if hasattr(security_manager, "raise_for_access"):
            try:
                await security_manager.raise_for_access(
                    datasource=datasource, user=current_user
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
            try:
                payload = await datasource.async_values_for_column(
                    column_name=column_name,
                    limit=1000,
                    rls_filters=rls_clauses or None,
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
            except KeyError as exc:
                raise SupersetValidationException(
                    f"Column name '{column_name}' does not exist"
                ) from exc
            except NotImplementedError as exc:
                raise SupersetValidationException(
                    f"Unable to get column values for "
                    f"datasource type: {datasource_type}"
                ) from exc

        # Fallback: check if column exists and return empty
        columns = getattr(datasource, "columns", None)
        if columns is not None:
            col_names = [getattr(c, "column_name", None) for c in columns]
            if column_name not in col_names:
                raise SupersetValidationException(
                    f"Column name '{column_name}' does not exist"
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
