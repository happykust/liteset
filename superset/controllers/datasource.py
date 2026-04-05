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

from litestar import Controller, get
from litestar.di import Provide

from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError, SupersetValidationException
from superset.guards.rbac import require_authentication
from superset.providers import provide_datasource_dao
from superset.typing import DatasourceDAOProtocol

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

        event_logger.log(
            "datasource.get",
            extra={"datasource_type": datasource_type, "datasource_id": datasource_id},
        )
        return {"result": result}

    @get(
        "/{datasource_type:str}/{datasource_id:int}/column/{column_name:str}/values/",
        guards=[require_authentication],
    )
    async def get_column_values(
        self,
        datasource_type: str,
        datasource_id: int,
        column_name: str,
        ds_dao: DatasourceDAOProtocol,
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

        # Try the values_for_column method on the datasource model
        if hasattr(datasource, "values_for_column"):
            try:
                payload = datasource.values_for_column(
                    column_name=column_name,
                    limit=1000,
                )
                event_logger.log(
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

        event_logger.log(
            "datasource.column_values",
            extra={
                "datasource_type": datasource_type,
                "datasource_id": datasource_id,
                "column_name": column_name,
            },
        )
        return {"result": []}
