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
"""TabState and TableSchema controllers — SQL Lab tab persistence.

Ports the original ``TabStateView`` and ``TableSchemaView``
to async Litestar controllers 1:1.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from litestar import Controller, delete, get, post, put, Request
from litestar.di import Provide
from litestar.response import Response
from sqlalchemy.ext.asyncio import AsyncSession

from superset.db.daos.tab_state import AsyncTableSchemaDAO, AsyncTabStateDAO
from superset.guards.rbac import require_permission
from superset.i18n import gettext as __
from superset.typing import UserProtocol
from superset.utils.core import error_msg_from_exception
from superset.utils.json import json_iso_dttm_ser as _json_iso_dttm_ser


def _provide_tab_state_dao(session: AsyncSession) -> AsyncTabStateDAO:
    return AsyncTabStateDAO(session)


def _provide_table_schema_dao(session: AsyncSession) -> AsyncTableSchemaDAO:
    return AsyncTableSchemaDAO(session)


logger = logging.getLogger(__name__)


class TabStateController(Controller):
    """SQL Lab tab state CRUD — mirrors the upstream TabStateView."""

    path = "/tabstateview"
    tags = ["SQL Lab"]
    dependencies = {
        "dao": Provide(_provide_tab_state_dao, sync_to_thread=False),
        "table_schema_dao": Provide(_provide_table_schema_dao, sync_to_thread=False),
    }

    @post("/", status_code=200, guards=[require_permission("can_post", "TabStateView")])
    async def create(
        self,
        request: Request[Any, Any, Any],
        dao: AsyncTabStateDAO,
        current_user: UserProtocol,
    ) -> Response[str]:
        """POST /tabstateview/ — create a new tab state.

        Upstream's view returns 200 via ``json_success`` (which sets
        no explicit status_code, so the WSGI layer defaults to 200). Override
        Litestar's @post default of 201 to match.
        """
        try:
            form = await request.form()
            query_editor = json.loads(form["queryEditor"])

            remote_id = query_editor.get("remoteId")

            # Set all user's existing tabs to inactive
            await dao.deactivate_all_for_user(current_user.id)

            tab_state = await dao.create_tab(
                {
                    "user_id": current_user.id,
                    "label": query_editor.get("name")
                    or query_editor.get("title", __("Untitled Query")),
                    "active": True,
                    "database_id": int(query_editor["dbId"]),
                    "catalog": query_editor.get("catalog"),
                    "schema": query_editor.get("schema"),
                    "sql": query_editor.get("sql", "SELECT ..."),
                    "query_limit": query_editor.get("queryLimit"),
                    "hide_left_bar": query_editor.get("hideLeftBar"),
                    "saved_query_id": int(remote_id) if remote_id is not None else None,
                    "template_params": query_editor.get("templateParams"),
                }
            )
            return Response(
                content=json.dumps({"id": tab_state.id}),
                media_type="application/json",
            )
        except Exception as ex:
            # Roll back the partial mutation before returning. The request
            # wrapper COMMITS on a returned Response, so without this a
            # multi-step handler (e.g. delete tab-state then table-schemas,
            # or delete-matching then create-schema) would persist its first
            # step when the second fails. 1:1 with upstream's
            # ``db.session.rollback()`` in every ``except``.
            await dao.session.rollback()
            return Response(
                content=json.dumps({"error": error_msg_from_exception(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @delete(
        "/{tab_state_id:int}",
        status_code=200,
        guards=[require_permission("can_delete", "TabStateView")],
    )
    async def delete_tab(
        self,
        tab_state_id: int,
        dao: AsyncTabStateDAO,
        table_schema_dao: AsyncTableSchemaDAO,
        current_user: UserProtocol,
    ) -> Response[str]:
        """DELETE /tabstateview/<id> — delete a tab state and its table schemas."""
        try:
            owner_id = await dao.get_owner_id(tab_state_id)
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
            # Delete tab state and its associated table schemas
            await dao.delete_by_id(tab_state_id)
            await table_schema_dao.delete_by_tab_state_id(tab_state_id)
            return Response(
                content=json.dumps("OK"),
                media_type="application/json",
            )
        except Exception as ex:
            # Roll back the partial mutation before returning. The request
            # wrapper COMMITS on a returned Response, so without this a
            # multi-step handler (e.g. delete tab-state then table-schemas,
            # or delete-matching then create-schema) would persist its first
            # step when the second fails. 1:1 with upstream's
            # ``db.session.rollback()`` in every ``except``.
            await dao.session.rollback()
            return Response(
                content=json.dumps({"error": error_msg_from_exception(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @get("/{tab_state_id:int}", guards=[require_permission("can_get", "TabStateView")])
    async def get_tab(
        self,
        tab_state_id: int,
        dao: AsyncTabStateDAO,
        current_user: UserProtocol,
    ) -> Response[str]:
        """GET /tabstateview/<id> — return a single tab state."""
        owner_id = await dao.get_owner_id(tab_state_id)
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

        tab_state = await dao.find_with_relations(tab_state_id)
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

    @post(
        "/{tab_state_id:int}/activate",
        status_code=200,
        guards=[require_permission("can_activate", "TabStateView")],
    )
    async def activate(
        self,
        tab_state_id: int,
        dao: AsyncTabStateDAO,
        current_user: UserProtocol,
    ) -> Response[str]:
        """POST /tabstateview/<id>/activate — activate a tab.

        Upstream returns 200 via ``json_success``; override Litestar default.
        """
        try:
            owner_id = await dao.get_owner_id(tab_state_id)
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
            await dao.activate_tab(current_user.id, tab_state_id)
            return Response(
                content=json.dumps(tab_state_id),
                media_type="application/json",
            )
        except Exception as ex:
            # Roll back the partial mutation before returning. The request
            # wrapper COMMITS on a returned Response, so without this a
            # multi-step handler (e.g. delete tab-state then table-schemas,
            # or delete-matching then create-schema) would persist its first
            # step when the second fails. 1:1 with upstream's
            # ``db.session.rollback()`` in every ``except``.
            await dao.session.rollback()
            return Response(
                content=json.dumps({"error": error_msg_from_exception(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @put("/{tab_state_id:int}", guards=[require_permission("can_put", "TabStateView")])
    async def update_tab(
        self,
        tab_state_id: int,
        request: Request[Any, Any, Any],
        dao: AsyncTabStateDAO,
        current_user: UserProtocol,
    ) -> Response[str]:
        """PUT /tabstateview/<id> — update tab state fields."""
        owner_id = await dao.get_owner_id(tab_state_id)
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

            await dao.update_fields(tab_state_id, fields)
            return Response(
                content=json.dumps(tab_state_id),
                media_type="application/json",
            )
        except Exception as ex:
            # Roll back the partial mutation before returning. The request
            # wrapper COMMITS on a returned Response, so without this a
            # multi-step handler (e.g. delete tab-state then table-schemas,
            # or delete-matching then create-schema) would persist its first
            # step when the second fails. 1:1 with upstream's
            # ``db.session.rollback()`` in every ``except``.
            await dao.session.rollback()
            return Response(
                content=json.dumps({"error": error_msg_from_exception(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @post(
        "/{tab_state_id:int}/migrate_query",
        status_code=200,
        guards=[require_permission("can_migrate_query", "TabStateView")],
    )
    async def migrate_query(
        self,
        tab_state_id: int,
        request: Request[Any, Any, Any],
        dao: AsyncTabStateDAO,
        current_user: UserProtocol,
    ) -> Response[str]:
        """POST /tabstateview/<id>/migrate_query — reassign a query to this tab."""
        try:
            owner_id = await dao.get_owner_id(tab_state_id)
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
            form = await request.form()
            client_id = json.loads(form["queryId"])

            await dao.migrate_query(client_id, tab_state_id)
            return Response(
                content=json.dumps(tab_state_id),
                media_type="application/json",
            )
        except Exception as ex:
            # Roll back the partial mutation before returning. The request
            # wrapper COMMITS on a returned Response, so without this a
            # multi-step handler (e.g. delete tab-state then table-schemas,
            # or delete-matching then create-schema) would persist its first
            # step when the second fails. 1:1 with upstream's
            # ``db.session.rollback()`` in every ``except``.
            await dao.session.rollback()
            return Response(
                content=json.dumps({"error": error_msg_from_exception(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @delete(
        "/{tab_state_id:int}/query/{client_id:str}",
        status_code=200,
        guards=[require_permission("can_delete_query", "TabStateView")],
    )
    async def delete_query(
        self,
        tab_state_id: int,
        client_id: str,
        dao: AsyncTabStateDAO,
        current_user: UserProtocol,
    ) -> Response[str]:
        """DELETE /tabstateview/<id>/query/<client_id> — remove a query from a tab."""
        try:
            # If this query was the tab's latest_query, replace with the previous one
            tab_state_match = await dao.find_tab_with_latest_query(
                tab_state_id, client_id
            )

            if tab_state_match is not None:
                prev_query = await dao.find_previous_query(
                    client_id, current_user.id, tab_state_id
                )

                await dao.replace_latest_query(
                    tab_state_id,
                    client_id,
                    prev_query.client_id if prev_query else None,
                )

            await dao.delete_query(client_id, current_user.id, tab_state_id)
            return Response(
                content=json.dumps("OK"),
                media_type="application/json",
            )
        except Exception as ex:
            # Roll back the partial mutation before returning. The request
            # wrapper COMMITS on a returned Response, so without this a
            # multi-step handler (e.g. delete tab-state then table-schemas,
            # or delete-matching then create-schema) would persist its first
            # step when the second fails. 1:1 with upstream's
            # ``db.session.rollback()`` in every ``except``.
            await dao.session.rollback()
            return Response(
                content=json.dumps({"error": error_msg_from_exception(ex)}),
                status_code=400,
                media_type="application/json",
            )


class TableSchemaController(Controller):
    """SQL Lab table schema CRUD — mirrors the upstream TableSchemaView."""

    path = "/tableschemaview"
    tags = ["SQL Lab"]
    dependencies = {
        "dao": Provide(_provide_table_schema_dao, sync_to_thread=False),
    }

    @post(
        "/",
        status_code=200,
        guards=[require_permission("can_post", "TableSchemaView")],
    )
    async def create(
        self,
        request: Request[Any, Any, Any],
        dao: AsyncTableSchemaDAO,
    ) -> Response[str]:
        """POST /tableschemaview/ — create or replace a table schema entry.

        Upstream returns 200 via ``json_success``; override Litestar default.
        """
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
                    content=json.dumps(
                        {"error": f"Missing required keys. Got: {list(table.keys())}"}
                    ),
                    status_code=400,
                    media_type="application/json",
                )
            ts_id = int(ts_id_raw)
            db_id = int(db_id_raw)

            # Delete existing schema with same params
            await dao.delete_matching(
                tab_state_id=ts_id,
                database_id=db_id,
                catalog=table.get("catalog"),
                schema=table["schema"],
                table=table["name"],
            )

            table_schema = await dao.create_schema(
                {
                    "tab_state_id": ts_id,
                    "database_id": db_id,
                    "catalog": table.get("catalog"),
                    "schema": table["schema"],
                    "table": table["name"],
                    "description": json.dumps(table),
                    "expanded": True,
                }
            )
            return Response(
                content=json.dumps({"id": table_schema.id}),
                media_type="application/json",
            )
        except Exception as ex:
            # Roll back the partial mutation before returning. The request
            # wrapper COMMITS on a returned Response, so without this a
            # multi-step handler (e.g. delete tab-state then table-schemas,
            # or delete-matching then create-schema) would persist its first
            # step when the second fails. 1:1 with upstream's
            # ``db.session.rollback()`` in every ``except``.
            await dao.session.rollback()
            return Response(
                content=json.dumps({"error": error_msg_from_exception(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @delete(
        "/{table_schema_id:int}",
        status_code=200,
        guards=[require_permission("can_delete", "TableSchemaView")],
    )
    async def delete_schema(
        self,
        table_schema_id: int,
        dao: AsyncTableSchemaDAO,
    ) -> Response[str]:
        """DELETE /tableschemaview/<id> — delete a table schema entry."""
        try:
            await dao.delete_by_id(table_schema_id)
            return Response(
                content=json.dumps("OK"),
                media_type="application/json",
            )
        except Exception as ex:
            # Roll back the partial mutation before returning. The request
            # wrapper COMMITS on a returned Response, so without this a
            # multi-step handler (e.g. delete tab-state then table-schemas,
            # or delete-matching then create-schema) would persist its first
            # step when the second fails. 1:1 with upstream's
            # ``db.session.rollback()`` in every ``except``.
            await dao.session.rollback()
            return Response(
                content=json.dumps({"error": error_msg_from_exception(ex)}),
                status_code=400,
                media_type="application/json",
            )

    @post(
        "/{table_schema_id:int}/expanded",
        status_code=200,
        guards=[require_permission("can_expanded", "TableSchemaView")],
    )
    async def set_expanded(
        self,
        table_schema_id: int,
        request: Request[Any, Any, Any],
        dao: AsyncTableSchemaDAO,
    ) -> Response[str]:
        """POST /tableschemaview/<id>/expanded — toggle expanded state.

        Matches original TableSchemaView.expanded: a missing ``expanded``
        form key raises the upstream ``BadRequestKeyError`` (an HTTP 400
        BadRequest subclass) which renders as 400 — NOT 500. Invalid
        JSON (``json.loads`` ValueError) propagates → 500, as upstream.
        """
        from litestar.exceptions import ClientException

        form = await request.form()
        if "expanded" not in form:
            # Upstream ImmutableMultiDict.__getitem__ → BadRequestKeyError → 400.
            raise ClientException(
                status_code=400, detail="Missing form key: 'expanded'"
            )
        payload = json.loads(form["expanded"])
        await dao.set_expanded(table_schema_id, payload)
        return Response(
            content=json.dumps({"id": table_schema_id, "expanded": payload}),
            media_type="application/json",
        )
