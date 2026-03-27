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

from superset.db.base_dao import BaseAsyncDAO
from superset.models.connectors import SqlaTable
from superset.models.core import Database
from superset.models.dashboard import Dashboard, dashboard_slices
from superset.models.slice import Slice
from superset.models.sql_lab import TabState


class AsyncDatabaseDAO(BaseAsyncDAO[Database]):
    model_cls = Database

    async def validate_uniqueness(self, database_name: str) -> bool:
        """Check that no database exists with the given name."""
        existing = await self.find_one_or_none(database_name=database_name)
        return existing is None

    async def validate_update_uniqueness(
        self,
        database_id: int,
        database_name: str,
    ) -> bool:
        """Check name uniqueness excluding the database being updated."""
        stmt = select(Database).where(
            Database.database_name == database_name,
            Database.id != database_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def get_database_by_name(
        self,
        database_name: str,
    ) -> Database | None:
        """Retrieve a database by name."""
        return await self.find_one_or_none(database_name=database_name)

    @staticmethod
    def build_db_for_connection_test(
        server_cert: str = "",
        extra: str = "",
        impersonate_user: bool = False,
        encrypted_extra: str = "",
    ) -> Database:
        """Create an ephemeral Database instance for connection testing.

        Does NOT persist to the database.
        """
        return Database(
            server_cert=server_cert,
            extra=extra,
            impersonate_user=impersonate_user,
            encrypted_extra=encrypted_extra,
        )

    async def get_related_objects(
        self,
        database_id: int,
    ) -> dict[str, list[Any]]:
        """Get charts, dashboards, and sqllab tab states related to a database."""
        dataset_stmt = select(SqlaTable.id).where(SqlaTable.database_id == database_id)
        dataset_result = await self.session.execute(dataset_stmt)
        dataset_ids = list(dataset_result.scalars().all())

        charts: list[Any] = []
        dashboards: list[Any] = []

        if dataset_ids:
            chart_stmt = select(Slice).where(
                Slice.datasource_id.in_(dataset_ids),
                Slice.datasource_type == "table",
            )
            chart_result = await self.session.execute(chart_stmt)
            charts = list(chart_result.scalars().all())

            chart_ids = [c.id for c in charts]
            if chart_ids:
                dash_stmt = (
                    select(Dashboard)
                    .join(
                        dashboard_slices,
                        Dashboard.id == dashboard_slices.c.dashboard_id,
                    )
                    .where(dashboard_slices.c.slice_id.in_(chart_ids))
                    .distinct()
                )
                dash_result = await self.session.execute(dash_stmt)
                dashboards = list(dash_result.scalars().all())

        # SQL Lab tab states linked to this database
        tab_stmt = select(TabState).where(TabState.database_id == database_id)
        tab_result = await self.session.execute(tab_stmt)
        sqllab_tab_states = list(tab_result.scalars().all())

        return {
            "charts": charts,
            "dashboards": dashboards,
            "sqllab_tab_states": sqllab_tab_states,
        }

    async def get_ssh_tunnel(self, database_id: int) -> Any | None:
        """Get SSH tunnel config for a database."""
        tunnel_dao = AsyncSSHTunnelDAO(self.session)
        return await tunnel_dao.get_by_database_id(database_id)

    async def get_datasets(
        self,
        database_id: int,
        catalog: str | None = None,
        schema: str | None = None,
    ) -> list[SqlaTable]:
        """Get datasets for a database, optionally filtered."""
        stmt = select(SqlaTable).where(SqlaTable.database_id == database_id)
        if catalog is not None:
            stmt = stmt.where(SqlaTable.catalog == catalog)
        if schema is not None:
            stmt = stmt.where(SqlaTable.schema == schema)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class AsyncSSHTunnelDAO:
    """Async DAO for SSH tunnel management.

    Does not inherit BaseAsyncDAO because the SSHTunnel model may not be
    available (optional superset dependency). Uses direct session queries
    with graceful ImportError fallback.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_database_id(self, database_id: int) -> Any | None:
        """Get SSH tunnel config for a database."""
        from superset.models.ssh_tunnel import SSHTunnel

        stmt = select(SSHTunnel).where(SSHTunnel.database_id == database_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()


class AsyncDatabaseUserOAuth2TokensDAO:
    """Async DAO for database user OAuth2 tokens.

    Does not inherit BaseAsyncDAO because it bypasses access filters
    and only provides direct session.get() for token retrieval.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_database(self, database_id: int) -> Database | None:
        """Get database without access filters (for OAuth2 token retrieval)."""
        return await self.session.get(Database, database_id)
