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
"""TabState and TableSchema controllers — SQL Lab tab persistence.

Ports the original Flask ``TabStateView`` and ``TableSchemaView``
to async Litestar controllers 1:1.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from litestar import Controller, delete, get, post, put, Request
from litestar.response import Response
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from superset.models.sql_lab import Query, TabState, TableSchema
from superset.typing import UserProtocol


def _json_iso_dttm_ser(obj: Any) -> str:
    """Serialize datetime objects to ISO format, matching original Superset."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)

logger = logging.getLogger(__name__)


async def _get_owner_id(
    session: AsyncSession, tab_state_id: int
) -> int | None:
    """Return the user_id that owns the given tab, or None if not found."""
    result = await session.execute(
        select(TabState.user_id).where(TabState.id == tab_state_id)
    )
    return result.scalar_one_or_none()


class TabStateController(Controller):
    """SQL Lab tab state CRUD — mirrors Flask TabStateView."""

    path = "/tabstateview"
    tags = ["SQL Lab"]

    @post("/")
    async def create(
        self,
        request: Request[Any, Any, Any],
        session: AsyncSession,
        current_user: UserProtocol,
    ) -> Response[str]:
        """POST /tabstateview/ — create a new tab state."""
        try:
            form = await request.form()
            query_editor = json.loads(form["queryEditor"])

            remote_id = query_editor.get("remoteId")
            tab_state = TabState(
                user_id=current_user.id,
                label=query_editor.get("name")
                or query_editor.get("title", "Untitled Query"),
                active=True,
                database_id=int(query_editor["dbId"]),
                catalog=query_editor.get("catalog"),
                schema=query_editor.get("schema"),
                sql=query_editor.get("sql", "SELECT ..."),
                query_limit=query_editor.get("queryLimit"),
                hide_left_bar=query_editor.get("hideLeftBar"),
                saved_query_id=int(remote_id) if remote_id is not None else None,
                template_params=query_editor.get("templateParams"),
            )

            # Set all user's existing tabs to inactive
            await session.execute(
                update(TabState)
                .where(TabState.user_id == current_user.id)
                .values(active=False)
            )

            session.add(tab_state)
            await session.flush()
            await session.commit()
            return Response(
                content=json.dumps({"id": tab_state.id}),
                media_type="application/json",
            )
        except Exception as ex:
            await session.rollback()
            return Response(
                content=json.dumps({"error": str(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @delete("/{tab_state_id:int}", status_code=200)
    async def delete_tab(
        self,
        tab_state_id: int,
        session: AsyncSession,
        current_user: UserProtocol,
    ) -> Response[str]:
        """DELETE /tabstateview/<id> — delete a tab state and its table schemas."""
        owner_id = await _get_owner_id(session, tab_state_id)
        if owner_id is None:
            return Response(
                content=json.dumps({"error": "Not found"}),
                status_code=404,
                media_type="application/json",
            )
        if owner_id != current_user.id:
            return Response(
                content=json.dumps({"error": "Forbidden"}),
                status_code=403,
                media_type="application/json",
            )

        try:
            # Delete tab state and its associated table schemas
            await session.execute(
                TabState.__table__.delete().where(TabState.id == tab_state_id)
            )
            await session.execute(
                TableSchema.__table__.delete().where(
                    TableSchema.tab_state_id == tab_state_id
                )
            )
            await session.commit()
            return Response(
                content=json.dumps("OK"),
                media_type="application/json",
            )
        except Exception as ex:
            await session.rollback()
            return Response(
                content=json.dumps({"error": str(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @get("/{tab_state_id:int}")
    async def get_tab(
        self,
        tab_state_id: int,
        session: AsyncSession,
        current_user: UserProtocol,
    ) -> Response[str]:
        """GET /tabstateview/<id> — return a single tab state."""
        owner_id = await _get_owner_id(session, tab_state_id)
        if owner_id is None:
            return Response(
                content=json.dumps({"error": "Not found"}),
                status_code=404,
                media_type="application/json",
            )
        if owner_id != current_user.id:
            return Response(
                content=json.dumps({"error": "Forbidden"}),
                status_code=403,
                media_type="application/json",
            )

        result = await session.execute(
            select(TabState)
            .where(TabState.id == tab_state_id)
            .options(
                selectinload(TabState.table_schemas),
                selectinload(TabState.latest_query),
                selectinload(TabState.saved_query),
            )
        )
        tab_state = result.scalars().first()
        if tab_state is None:
            return Response(
                content=json.dumps({"error": "Not found"}),
                status_code=404,
                media_type="application/json",
            )
        return Response(
            content=json.dumps(tab_state.to_dict(), default=_json_iso_dttm_ser),
            media_type="application/json",
        )

    @post("/{tab_state_id:int}/activate")
    async def activate(
        self,
        tab_state_id: int,
        session: AsyncSession,
        current_user: UserProtocol,
    ) -> Response[str]:
        """POST /tabstateview/<id>/activate — activate a tab."""
        owner_id = await _get_owner_id(session, tab_state_id)
        if owner_id is None:
            return Response(
                content=json.dumps({"error": "Not found"}),
                status_code=404,
                media_type="application/json",
            )
        if owner_id != current_user.id:
            return Response(
                content=json.dumps({"error": "Forbidden"}),
                status_code=403,
                media_type="application/json",
            )

        try:
            await session.execute(
                update(TabState)
                .where(TabState.user_id == current_user.id)
                .values(active=TabState.id == tab_state_id)
            )
            await session.commit()
            return Response(
                content=json.dumps(tab_state_id),
                media_type="application/json",
            )
        except Exception as ex:
            await session.rollback()
            return Response(
                content=json.dumps({"error": str(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @put("/{tab_state_id:int}")
    async def update_tab(
        self,
        tab_state_id: int,
        request: Request[Any, Any, Any],
        session: AsyncSession,
        current_user: UserProtocol,
    ) -> Response[str]:
        """PUT /tabstateview/<id> — update tab state fields."""
        owner_id = await _get_owner_id(session, tab_state_id)
        if owner_id is None:
            return Response(
                content=json.dumps({"error": "Not found"}),
                status_code=404,
                media_type="application/json",
            )
        if owner_id != current_user.id:
            return Response(
                content=json.dumps({"error": "Forbidden"}),
                status_code=403,
                media_type="application/json",
            )

        try:
            form = await request.form()
            fields = {k: json.loads(v) for k, v in dict(form).items()}

            await session.execute(
                update(TabState)
                .where(TabState.id == tab_state_id)
                .values(**fields)
            )
            await session.commit()
            return Response(
                content=json.dumps(tab_state_id),
                media_type="application/json",
            )
        except Exception as ex:
            await session.rollback()
            return Response(
                content=json.dumps({"error": str(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @post("/{tab_state_id:int}/migrate_query")
    async def migrate_query(
        self,
        tab_state_id: int,
        request: Request[Any, Any, Any],
        session: AsyncSession,
        current_user: UserProtocol,
    ) -> Response[str]:
        """POST /tabstateview/<id>/migrate_query — reassign a query to this tab."""
        owner_id = await _get_owner_id(session, tab_state_id)
        if owner_id is None:
            return Response(
                content=json.dumps({"error": "Not found"}),
                status_code=404,
                media_type="application/json",
            )
        if owner_id != current_user.id:
            return Response(
                content=json.dumps({"error": "Forbidden"}),
                status_code=403,
                media_type="application/json",
            )

        try:
            form = await request.form()
            client_id = json.loads(form["queryId"])

            await session.execute(
                update(Query)
                .where(Query.client_id == client_id)
                .values(sql_editor_id=tab_state_id)
            )
            await session.commit()
            return Response(
                content=json.dumps(tab_state_id),
                media_type="application/json",
            )
        except Exception as ex:
            await session.rollback()
            return Response(
                content=json.dumps({"error": str(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @delete("/{tab_state_id:int}/query/{client_id:str}", status_code=200)
    async def delete_query(
        self,
        tab_state_id: int,
        client_id: str,
        session: AsyncSession,
        current_user: UserProtocol,
    ) -> Response[str]:
        """DELETE /tabstateview/<id>/query/<client_id> — remove a query from a tab."""
        owner_id = await _get_owner_id(session, tab_state_id)
        if owner_id is None:
            return Response(
                content=json.dumps({"error": "Not found"}),
                status_code=404,
                media_type="application/json",
            )
        if owner_id != current_user.id:
            return Response(
                content=json.dumps({"error": "Forbidden"}),
                status_code=403,
                media_type="application/json",
            )

        try:
            # If this query was the tab's latest_query, replace with the previous one
            tab_state_result = await session.execute(
                select(TabState).where(
                    TabState.id == tab_state_id,
                    TabState.latest_query_id == client_id,
                )
            )
            tab_state_match = tab_state_result.scalars().first()

            if tab_state_match is not None:
                prev_query_result = await session.execute(
                    select(Query)
                    .where(
                        and_(
                            Query.client_id != client_id,
                            Query.user_id == current_user.id,
                            Query.sql_editor_id == str(tab_state_id),
                        )
                    )
                    .order_by(Query.id.desc())
                    .limit(1)
                )
                prev_query = prev_query_result.scalars().first()

                await session.execute(
                    update(TabState)
                    .where(
                        TabState.id == tab_state_id,
                        TabState.latest_query_id == client_id,
                    )
                    .values(
                        latest_query_id=prev_query.client_id
                    if prev_query
                    else None
                )
            )

            await session.execute(
                Query.__table__.delete().where(
                    and_(
                        Query.client_id == client_id,
                        Query.user_id == current_user.id,
                        Query.sql_editor_id == str(tab_state_id),
                    )
                )
            )
            await session.commit()
            return Response(
                content=json.dumps("OK"),
                media_type="application/json",
            )
        except Exception as ex:
            await session.rollback()
            return Response(
                content=json.dumps({"error": str(ex)}),
                status_code=400,
                media_type="application/json",
            )


class TableSchemaController(Controller):
    """SQL Lab table schema CRUD — mirrors Flask TableSchemaView."""

    path = "/tableschemaview"
    tags = ["SQL Lab"]

    @post("/")
    async def create(
        self,
        request: Request[Any, Any, Any],
        session: AsyncSession,
    ) -> Response[str]:
        """POST /tableschemaview/ — create or replace a table schema entry."""
        try:
            form = await request.form()
            raw_table = form.get("table", "{}")
            # SupersetClient sends postPayload values via JSON.stringify;
            # the form value may be a plain string or bytes.
            if isinstance(raw_table, bytes):
                raw_table = raw_table.decode()
            table = json.loads(str(raw_table))

            ts_id_raw = table.get("queryEditorId") or table.get("tab_state_id")
            db_id_raw = table.get("dbId") or table.get("database_id")
            if ts_id_raw is None or db_id_raw is None:
                logger.error(
                    "TableSchema POST: missing keys. Got keys=%s",
                    list(table.keys()),
                )
                return Response(
                    content=json.dumps({
                        "error": f"Missing required keys. Got: {list(table.keys())}"
                    }),
                    status_code=400,
                    media_type="application/json",
                )
            ts_id = int(ts_id_raw)
            db_id = int(db_id_raw)

            # Delete existing schema with same params
            await session.execute(
                TableSchema.__table__.delete().where(
                    and_(
                        TableSchema.tab_state_id == ts_id,
                        TableSchema.database_id == db_id,
                        TableSchema.catalog == table.get("catalog"),
                        TableSchema.schema == table["schema"],
                        TableSchema.table == table["name"],
                    )
                )
            )

            table_schema = TableSchema(
                tab_state_id=ts_id,
                database_id=db_id,
                catalog=table.get("catalog"),
                schema=table["schema"],
                table=table["name"],
                description=json.dumps(table),
                expanded=True,
            )
            session.add(table_schema)
            await session.flush()
            await session.commit()
            return Response(
                content=json.dumps({"id": table_schema.id}),
                media_type="application/json",
            )
        except Exception as ex:
            await session.rollback()
            return Response(
                content=json.dumps({"error": str(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @delete("/{table_schema_id:int}", status_code=200)
    async def delete_schema(
        self,
        table_schema_id: int,
        session: AsyncSession,
    ) -> Response[str]:
        """DELETE /tableschemaview/<id> — delete a table schema entry."""
        try:
            await session.execute(
                TableSchema.__table__.delete().where(
                    TableSchema.id == table_schema_id
                )
            )
            await session.commit()
            return Response(
                content=json.dumps("OK"),
                media_type="application/json",
            )
        except Exception as ex:
            await session.rollback()
            return Response(
                content=json.dumps({"error": str(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @post("/{table_schema_id:int}/expanded")
    async def set_expanded(
        self,
        table_schema_id: int,
        request: Request[Any, Any, Any],
        session: AsyncSession,
    ) -> Response[str]:
        """POST /tableschemaview/<id>/expanded — toggle expanded state."""
        try:
            form = await request.form()
            payload = json.loads(form["expanded"])

            await session.execute(
                update(TableSchema)
                .where(TableSchema.id == table_schema_id)
                .values(expanded=payload)
            )
            await session.commit()
            return Response(
                content=json.dumps({"id": table_schema_id, "expanded": payload}),
                media_type="application/json",
            )
        except Exception as ex:
            await session.rollback()
            return Response(
                content=json.dumps({"error": str(ex)}),
                status_code=400,
                media_type="application/json",
            )
