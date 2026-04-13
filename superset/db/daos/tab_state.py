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
# mypy: ignore-errors
"""Async DAOs for SQL Lab tab state and table schema persistence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.orm import selectinload

from superset.db.base_dao import BaseAsyncDAO
from superset.models.sql_lab import Query, TableSchema, TabState


class AsyncTabStateDAO(BaseAsyncDAO[TabState]):
    """Data-access layer for the ``tab_state`` table."""

    model_cls = TabState

    async def get_owner_id(self, tab_state_id: int) -> int | None:
        """Return the user_id that owns the given tab, or None if not found."""
        result = await self.session.execute(
            select(TabState.user_id).where(TabState.id == tab_state_id)
        )
        return result.scalar_one_or_none()

    async def deactivate_all_for_user(self, user_id: int) -> None:
        """Set all tabs belonging to *user_id* to inactive."""
        await self.session.execute(
            update(TabState).where(TabState.user_id == user_id).values(active=False)
        )

    async def create_tab(self, attributes: dict[str, Any]) -> TabState:
        """Insert a new tab state and flush to obtain its generated id."""
        tab_state = TabState(**attributes)
        self.session.add(tab_state)
        await self.session.flush()
        return tab_state

    async def delete_by_id(self, tab_state_id: int) -> None:
        """Hard-delete a tab state row by primary key (core table DELETE)."""
        await self.session.execute(
            TabState.__table__.delete().where(TabState.id == tab_state_id)
        )

    async def find_with_relations(self, tab_state_id: int) -> TabState | None:
        """Load a tab state with table_schemas, latest_query, and saved_query."""
        result = await self.session.execute(
            select(TabState)
            .where(TabState.id == tab_state_id)
            .options(
                selectinload(TabState.table_schemas),
                selectinload(TabState.latest_query),
                selectinload(TabState.saved_query),
            )
        )
        return result.scalars().first()

    async def activate_tab(self, user_id: int, tab_state_id: int) -> None:
        """Set only *tab_state_id* as active for *user_id*; deactivate others."""
        await self.session.execute(
            update(TabState)
            .where(TabState.user_id == user_id)
            .values(active=TabState.id == tab_state_id)
        )

    async def update_fields(self, tab_state_id: int, fields: dict[str, Any]) -> None:
        """Bulk-update arbitrary fields on a tab state row."""
        await self.session.execute(
            update(TabState).where(TabState.id == tab_state_id).values(**fields)
        )

    async def migrate_query(self, client_id: str, tab_state_id: int) -> None:
        """Reassign a query (by *client_id*) to *tab_state_id*."""
        await self.session.execute(
            update(Query)
            .where(Query.client_id == client_id)
            .values(sql_editor_id=tab_state_id)
        )

    async def find_tab_with_latest_query(
        self, tab_state_id: int, client_id: str
    ) -> TabState | None:
        """Return the tab if its latest_query_id matches *client_id*."""
        result = await self.session.execute(
            select(TabState).where(
                TabState.id == tab_state_id,
                TabState.latest_query_id == client_id,
            )
        )
        return result.scalars().first()

    async def find_previous_query(
        self,
        client_id: str,
        user_id: int,
        tab_state_id: int,
    ) -> Query | None:
        """Find the most recent query for *tab_state_id* excluding *client_id*."""
        result = await self.session.execute(
            select(Query)
            .where(
                and_(
                    Query.client_id != client_id,
                    Query.user_id == user_id,
                    Query.sql_editor_id == str(tab_state_id),
                )
            )
            .order_by(Query.id.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def replace_latest_query(
        self,
        tab_state_id: int,
        old_client_id: str,
        new_client_id: str | None,
    ) -> None:
        """Swap the latest_query_id on a tab from *old_client_id* to *new_client_id*."""
        await self.session.execute(
            update(TabState)
            .where(
                TabState.id == tab_state_id,
                TabState.latest_query_id == old_client_id,
            )
            .values(latest_query_id=new_client_id)
        )

    async def delete_query(
        self,
        client_id: str,
        user_id: int,
        tab_state_id: int,
    ) -> None:
        """Hard-delete a query row scoped to user + tab."""
        await self.session.execute(
            Query.__table__.delete().where(
                and_(
                    Query.client_id == client_id,
                    Query.user_id == user_id,
                    Query.sql_editor_id == str(tab_state_id),
                )
            )
        )

    async def get_tab_state_ids(self, user_id: int) -> list[dict[str, Any]]:
        """Return ``[{id, label}, ...]`` for all tabs owned by *user_id*."""
        stmt = select(TabState.id, TabState.label).where(
            TabState.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return [{"id": row.id, "label": row.label} for row in result.all()]

    async def get_active_tab(self, user_id: int) -> dict[str, Any] | None:
        """Return the active (or first) tab for *user_id*, eager-loading relations."""
        stmt = (
            select(TabState)
            .where(TabState.user_id == user_id)
            .order_by(TabState.active.desc())
            .limit(1)
            .options(
                selectinload(TabState.table_schemas),
                selectinload(TabState.latest_query),
                selectinload(TabState.saved_query),
            )
        )
        result = await self.session.execute(stmt)
        row = result.scalars().first()
        if row is None:
            return None
        return (
            row.to_dict()
            if hasattr(row, "to_dict")
            else {"id": row.id, "label": row.label}
        )


class AsyncTableSchemaDAO(BaseAsyncDAO[TableSchema]):
    """Data-access layer for the ``table_schema`` table."""

    model_cls = TableSchema

    async def delete_matching(
        self,
        tab_state_id: int,
        database_id: int,
        catalog: str | None,
        schema: str,
        table: str,
    ) -> None:
        """Delete existing schema rows matching the exact key tuple."""
        await self.session.execute(
            TableSchema.__table__.delete().where(
                and_(
                    TableSchema.tab_state_id == tab_state_id,
                    TableSchema.database_id == database_id,
                    TableSchema.catalog == catalog,
                    TableSchema.schema == schema,
                    TableSchema.table == table,
                )
            )
        )

    async def create_schema(self, attributes: dict[str, Any]) -> TableSchema:
        """Insert a new table schema and flush to obtain its generated id."""
        table_schema = TableSchema(**attributes)
        self.session.add(table_schema)
        await self.session.flush()
        return table_schema

    async def delete_by_id(self, table_schema_id: int) -> None:
        """Hard-delete a table schema row by primary key."""
        await self.session.execute(
            TableSchema.__table__.delete().where(TableSchema.id == table_schema_id)
        )

    async def delete_by_tab_state_id(self, tab_state_id: int) -> None:
        """Delete all table schema rows associated with a tab state."""
        await self.session.execute(
            TableSchema.__table__.delete().where(
                TableSchema.tab_state_id == tab_state_id
            )
        )

    async def set_expanded(self, table_schema_id: int, expanded: bool) -> None:
        """Update the expanded flag on a table schema row."""
        await self.session.execute(
            update(TableSchema)
            .where(TableSchema.id == table_schema_id)
            .values(expanded=expanded)
        )
