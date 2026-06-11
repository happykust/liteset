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
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from superset.db.base_dao import BaseAsyncDAO
from superset.models.connectors import SqlaTable
from superset.models.core import Database
from superset.models.dashboard import Dashboard, dashboard_slices
from superset.models.slice import Slice
from superset.models.sql_lab import TabState

logger = logging.getLogger(__name__)

# Sentinel for ``get_datasets``: distinguishes "argument omitted → no filter"
# (the export flow wants every dataset) from "explicit None → IS NULL"
# (upstream's unconditional filter semantics; see R13-08).
_UNSET: Any = object()


class AsyncDatabaseDAO(BaseAsyncDAO[Database]):
    model_cls = Database

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
        if item is not None and attributes and "encrypted_extra" in attributes:
            # Delegate to the engine spec, 1:1 with upstream
            # ``item.db_engine_spec.unmask_encrypted_extra(old, new)`` — each
            # spec knows its own sensitive fields (e.g. BigQuery ``private_key``,
            # GSheets credentials) instead of a blanket ``{"$.*"}`` reveal.
            attributes["encrypted_extra"] = item.db_engine_spec.unmask_encrypted_extra(
                cast("str | None", item.encrypted_extra),
                attributes["encrypted_extra"],
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

    async def has_dependent_datasets(self, database_id: int) -> bool:
        """Return ``True`` when at least one dataset is attached to the database.

        1:1 with the original ``DeleteDatabaseCommand`` check
        (``self._model.tables`` truthiness): a single matching ``SqlaTable``
        row is enough to block deletion.
        """
        stmt = select(SqlaTable.id).where(SqlaTable.database_id == database_id).limit(1)
        result = await self.session.execute(stmt)
        return result.scalars().first() is not None

    async def get_table_extra_lookup(
        self,
        database_id: int,
        table_names: set[str],
        schema: str | None = None,
        catalog: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return a mapping of table_name to parsed extra JSON for the given tables.

        Used to enrich table/view listings with certification info. 1:1 with
        the original ``TablesDatabaseCommand``'s ``extra_dict_by_name`` query,
        which filters by ``database_id`` + ``catalog`` + ``schema`` and parses
        ``SqlaTable.extra`` via ``extra_dict`` (empty dict on parse failure).
        """
        if not table_names:
            return {}
        import json

        # Filter catalog/schema UNCONDITIONALLY (None → ``IS NULL``), 1:1 with
        # upstream which filters ``SqlaTable.catalog == self._catalog_name`` /
        # ``... .schema == self._schema_name`` regardless of None. Skipping the
        # filter when None (the common single-catalog case) would match rows
        # from other catalogs/schemas that share a table name.
        stmt = select(SqlaTable.table_name, SqlaTable.extra).where(
            SqlaTable.database_id == database_id,
            SqlaTable.table_name.in_(table_names),
            SqlaTable.catalog == catalog,
            SqlaTable.schema == schema,
        )
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
        catalog: str | None | object = _UNSET,
        schema: str | None | object = _UNSET,
    ) -> list[SqlaTable]:
        """Get datasets for a database, optionally filtered.

        ``catalog``/``schema`` filtering is UNCONDITIONAL once the argument
        is supplied — ``None`` compiles to ``IS NULL``, 1:1 with upstream
        ``DatabaseDAO.get_datasets`` (R13-08: the previous conditional
        semantics made ``SyncPermissionsCommand`` rewrite perms on datasets
        of ALL catalogs when ``catalog=None``). Callers that want every
        dataset regardless of catalog/schema (the export flow) simply omit
        the arguments — upstream export reads ``model.tables`` and never
        goes through this method, so the omitted-argument contract is local
        to the port.

        Eager-loads ``metrics`` and ``columns`` because the database export
        flow calls ``dataset.export_to_dict(recursive=True)`` on every row
        — that method walks ``SqlaTable.export_children`` and would otherwise
        trigger a synchronous lazy-load that crashes with ``MissingGreenlet``
        outside the AsyncSession greenlet.
        """
        from sqlalchemy.orm import selectinload

        stmt = (
            select(SqlaTable)
            .where(SqlaTable.database_id == database_id)
            .options(
                selectinload(SqlaTable.metrics),
                selectinload(SqlaTable.columns),
            )
        )
        if catalog is not _UNSET:
            stmt = stmt.where(SqlaTable.catalog == catalog)
        if schema is not _UNSET:
            stmt = stmt.where(SqlaTable.schema == schema)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class AsyncSSHTunnelDAO:
    """Async DAO for SSH tunnel management.

    Does not inherit BaseAsyncDAO because the SSHTunnel model may not be
    available (optional superset dependency). Uses direct session queries
    with graceful ImportError fallback.

    Mirrors ``superset_old/daos/database.py::SSHTunnelDAO``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_database_id(self, database_id: int) -> Any | None:
        """Get SSH tunnel config for a database."""
        from superset.models.ssh_tunnel import SSHTunnel

        stmt = select(SSHTunnel).where(SSHTunnel.database_id == database_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> Any:
        """Create a new SSHTunnel row.

        Port of ``BaseDAO.create`` specialised for SSHTunnel.
        """
        from superset.models.ssh_tunnel import SSHTunnel

        tunnel = SSHTunnel(**attributes)
        self.session.add(tunnel)
        await self.session.flush()
        return tunnel

    async def update(
        self,
        item: Any,
        attributes: dict[str, Any],
    ) -> Any:
        """Update an SSHTunnel, unmasking credential fields before persisting.

        When a database is edited the user sees masked values for
        ``password``, ``private_key``, and ``private_key_password`` (the
        ``PASSWORD_MASK`` sentinel).  Before writing we replace any mask
        sentinels with the values already stored on the model so the real
        secrets are not overwritten.

        Port of ``superset_old/daos/database.py::SSHTunnelDAO.update``.
        """
        from superset.utils.ssh_tunnel import unmask_password_info

        # ID cannot be updated — matches the original.
        attributes.pop("id", None)
        attributes = unmask_password_info(attributes, item)

        for key, value in attributes.items():
            setattr(item, key, value)
        await self.session.flush()
        return item

    async def delete(self, item: Any) -> None:
        """Delete an SSHTunnel row.

        Port of ``BaseDAO.delete`` specialised for SSHTunnel.
        """
        await self.session.delete(item)
        await self.session.flush()


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
