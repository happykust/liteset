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

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, selectinload

from superset.models.connectors import SqlaTable
from superset.models.sql_lab import Query, SavedQuery

_DATASOURCE_TYPE_MAP: dict[str, type[DeclarativeBase]] = {
    "table": SqlaTable,
    "query": Query,
    "saved_query": SavedQuery,
}


class AsyncDatasourceDAO:
    """Polymorphic async DAO for datasource access.

    Does not inherit BaseAsyncDAO because it dispatches to different
    model classes based on datasource_type rather than being bound to
    a single model.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_datasource(
        self,
        datasource_type: str,
        datasource_id: int,
    ) -> Any | None:
        """Get a datasource by type and ID.

        Supports 'table' (SqlaTable), 'query' (Query), and
        'saved_query' (SavedQuery) types. Returns None if not found.

        All returned instances have ``database`` (and type-specific
        relationships) eager-loaded to avoid MissingGreenlet when
        downstream code accesses them synchronously.  ``populate_existing``
        forces re-population of options if the instance is already in the
        session's identity map from an earlier load that didn't use these
        options.
        """
        model_cls = _DATASOURCE_TYPE_MAP.get(datasource_type)
        if model_cls is None:
            # Raises ``DatasourceTypeNotSupportedError`` (status 422) — the enum
            # also contains "view"/"dataset" which have no backing model;
            # a bare ValueError would surface as HTTP 500.
            from superset.exceptions import DatasourceTypeNotSupportedError

            raise DatasourceTypeNotSupportedError()
        stmt: Any
        if model_cls is SqlaTable:
            stmt = (
                select(SqlaTable)
                .where(SqlaTable.id == datasource_id)
                .options(
                    selectinload(SqlaTable.database),
                    selectinload(SqlaTable.columns),
                    selectinload(SqlaTable.metrics),
                    selectinload(SqlaTable.owners),
                )
                .execution_options(populate_existing=True)
            )
        elif model_cls is Query:
            stmt = (
                select(Query)
                .where(Query.id == datasource_id)
                .options(selectinload(Query.database))
                .execution_options(populate_existing=True)
            )
        else:  # SavedQuery
            stmt = (
                select(SavedQuery)
                .where(SavedQuery.id == datasource_id)
                .options(selectinload(SavedQuery.database))
                .execution_options(populate_existing=True)
            )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()
