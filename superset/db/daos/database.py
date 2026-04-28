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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from superset.db.base_dao import BaseAsyncDAO
from superset.models.connectors import SqlaTable
from superset.models.core import Database
from superset.models.dashboard import Dashboard, dashboard_slices
from superset.models.slice import Slice
from superset.models.sql_lab import TabState
from superset.utils.json import dumps, loads, reveal_sensitive

logger = logging.getLogger(__name__)


class AsyncDatabaseDAO(BaseAsyncDAO[Database]):
    model_cls = Database

    # Default JSONPath pattern matching all top-level keys in encrypted_extra.
    # This matches BaseEngineSpec.encrypted_extra_sensitive_fields from the
    # original Superset, ensuring masked values are properly unmasked before
    # persisting updates.
    _encrypted_extra_sensitive_fields: set[str] = {"$.*"}  # noqa: RUF012

    async def update(
        self,
        item: Database,
        attributes: dict[str, Any],
    ) -> Database:
        """
        Unmask ``encrypted_extra`` before updating.

        When a database is edited the user sees a masked version of
        ``encrypted_extra``. The masked values should be unmasked before the
        database is updated so that the original sensitive data is preserved.

        Ports the original ``DatabaseDAO.update`` logic.
        """
        if "encrypted_extra" in attributes:
            old_encrypted = item.encrypted_extra
            new_encrypted = attributes["encrypted_extra"]

            if old_encrypted is not None and new_encrypted is not None:
                try:
                    old_config = loads(old_encrypted)  # type: ignore[arg-type]
                    new_config = loads(new_encrypted)
                    new_config = reveal_sensitive(
                        old_config,
                        new_config,
                        self._encrypted_extra_sensitive_fields,
                    )
                    attributes["encrypted_extra"] = dumps(new_config)
                except (TypeError, ValueError):
                    # If JSON parsing fails, pass through the new value as-is
                    logger.warning(
                        "Could not unmask encrypted_extra during database update"
                    )

        return await super().update(item, attributes)

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

    async def get_table_extra_lookup(
        self,
        database_id: int,
        table_names: set[str],
        schema: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return a mapping of table_name to parsed extra JSON for the given tables.

        Used to enrich table/view listings with certification info.
        """
        if not table_names:
            return {}
        import json

        stmt = select(SqlaTable.table_name, SqlaTable.extra).where(
            SqlaTable.database_id == database_id,
            SqlaTable.table_name.in_(table_names),
        )
        if schema:
            stmt = stmt.where(SqlaTable.schema == schema)
        rows = (await self.session.execute(stmt)).all()
        result: dict[str, dict[str, Any]] = {}
        for tbl_name, extra_raw in rows:
            if extra_raw:
                try:
                    result[tbl_name] = json.loads(extra_raw)
                except (json.JSONDecodeError, TypeError):
                    pass
        return result

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

    async def find_one_or_none(
        self,
        *,
        user_id: int,
        database_id: int,
    ) -> Any | None:
        """Return the OAuth2 token row for ``(user_id, database_id)`` or None."""
        from superset.models.core import DatabaseUserOAuth2Tokens

        stmt = select(DatabaseUserOAuth2Tokens).where(
            DatabaseUserOAuth2Tokens.user_id == user_id,
            DatabaseUserOAuth2Tokens.database_id == database_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def delete(self, token: Any) -> None:
        """Delete an OAuth2 token row."""
        await self.session.delete(token)
        await self.session.flush()

    async def create(self, attributes: dict[str, Any]) -> Any:
        """Create a new OAuth2 token row."""
        from superset.models.core import DatabaseUserOAuth2Tokens

        token = DatabaseUserOAuth2Tokens(**attributes)
        self.session.add(token)
        await self.session.flush()
        return token
