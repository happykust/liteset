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
"""Row Level Security controller — CRUD endpoints for RLS filters.

Mirrors ``superset_old.row_level_security.api.RLSRestApi`` 1:1:

* GET    ``/`` — list RLS filters
* GET    ``/_info`` — API metadata
* GET    ``/{pk}`` — fetch a single RLS filter
* GET    ``/related/{column_name}`` — related-field lookup for the UI
* POST   ``/`` — create an RLS filter (returns ``{id, result: payload}``)
* PUT    ``/{pk}`` — partial update (returns ``{id, result: payload}``)
* DELETE ``/?q=[ids]`` — bulk-delete (the only delete endpoint, exactly
  as in original Superset; there is no ``DELETE /{pk}``)
"""

from __future__ import annotations

from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.di import Provide
from sqlalchemy.orm import selectinload

from superset.commands.security.create import CreateRLSRuleCommand
from superset.commands.security.delete import DeleteRLSRuleCommand
from superset.commands.security.update import UpdateRLSRuleCommand
from superset.controllers.base import (
    build_rison_query_params,
    extract_ids,
    get_info_payload,
    get_related_payload,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_rls_dao
from superset.schemas.rls import RLSPostSchema, RLSPutSchema
from superset.typing import CRUDDAOProtocol
from superset.utils import filter_unset


def _msgspec_to_dict(obj: Any) -> dict[str, Any]:
    """Convert a msgspec Struct to a plain dict."""
    return {f: getattr(obj, f) for f in obj.__struct_fields__}


def _prettify_label(name: str) -> str:
    """Convert a column name to a human-readable label.

    Local copy of ``superset.controllers.base._prettify_column`` to avoid
    a private-symbol import cycle.
    """
    return name.replace(".", " ").replace("_", " ").title()


# Mirrors ``superset_old/row_level_security/api.py:list_columns``.
_RLS_LIST_COLUMNS: list[str] = [
    "id",
    "name",
    "filter_type",
    "tables.id",
    "tables.table_name",
    "roles.id",
    "roles.name",
    "clause",
    "changed_on_delta_humanized",
    "changed_by.first_name",
    "changed_by.last_name",
    "changed_by.id",
    "group_key",
]

# Mirrors ``superset_old/row_level_security/api.py:order_columns``.
_RLS_ORDER_COLUMNS: list[str] = [
    "name",
    "filter_type",
    "clause",
    "changed_on_delta_humanized",
    "group_key",
]


def _serialize_rls_list_item(item: Any) -> dict[str, Any]:
    """Serialize an RLS rule for the list endpoint.

    Mirrors the FAB-generated payload built from
    ``superset_old/row_level_security/api.py::RLSRestApi.list_columns``.
    """
    changed_by = getattr(item, "changed_by", None)
    return {
        "id": item.id,
        "name": item.name,
        "filter_type": item.filter_type,
        "clause": item.clause,
        "group_key": item.group_key,
        "changed_on_delta_humanized": (
            getattr(item, "changed_on_delta_humanized", None) or ""
        ),
        "tables": [
            {"id": t.id, "table_name": t.table_name}
            for t in (getattr(item, "tables", None) or [])
        ],
        "roles": [
            {"id": r.id, "name": r.name} for r in (getattr(item, "roles", None) or [])
        ],
        "changed_by": (
            {
                "id": changed_by.id,
                "first_name": getattr(changed_by, "first_name", ""),
                "last_name": getattr(changed_by, "last_name", ""),
            }
            if changed_by is not None
            else None
        ),
    }


def _serialize_rls_show_item(item: Any) -> dict[str, Any]:
    """Serialize an RLS rule for the GET ``/{pk}`` endpoint.

    Mirrors ``superset_old/row_level_security/schemas.py::RLSShowSchema``
    via ``superset_old/row_level_security/api.py::show_columns``.
    """
    return {
        "id": item.id,
        "name": item.name,
        "description": getattr(item, "description", None),
        "filter_type": item.filter_type,
        "tables": [
            {
                "id": t.id,
                "schema": getattr(t, "schema", None),
                "table_name": t.table_name,
            }
            for t in (getattr(item, "tables", None) or [])
        ],
        "roles": [
            {"id": r.id, "name": r.name} for r in (getattr(item, "roles", None) or [])
        ],
        "group_key": item.group_key,
        "clause": item.clause,
    }


class RLSController(Controller):
    path = "/api/v1/rowlevelsecurity"
    tags = ["Row Level Security"]
    dependencies = {
        "dao": Provide(provide_rls_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET — list RLS filters
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "Row Level Security")],
    )
    async def get_list(
        self,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Get a list of Row Level Security filters.

        Supports Rison-encoded ``q`` query parameters for filtering
        (``filters: [{col, opr, value}]``), ordering (``order_column`` /
        ``order_direction``), and pagination (``page`` / ``page_size``)
        — matching ``superset_old/row_level_security/api.py`` 1:1 via
        ``search_columns = (name, description, filter_type, tables,
        roles, group_key, clause, created_by, changed_by)`` and
        ``order_columns = [name, filter_type, clause,
        changed_on_delta_humanized, group_key]``.
        """
        from superset.models.connectors import RowLevelSecurityFilter

        rison_filters, order_by, page, page_size = build_rison_query_params(
            RowLevelSecurityFilter,
            rison_params,
        )
        items = await dao.find_all(
            filters=rison_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(RowLevelSecurityFilter.tables),
                selectinload(RowLevelSecurityFilter.roles),
                selectinload(RowLevelSecurityFilter.changed_by),
            ],
        )
        total = await dao.count(filters=rison_filters or None)
        await event_logger.alog_with_context("rls.list")

        result = [_serialize_rls_list_item(item) for item in items]
        ids = [str(item.id) for item in items]
        label_columns = {col: _prettify_label(col) for col in _RLS_LIST_COLUMNS}
        return {
            "count": total,
            "description_columns": {},
            "ids": ids,
            "label_columns": label_columns,
            "list_columns": list(_RLS_LIST_COLUMNS),
            "list_title": "List Row Level Security",
            "order_columns": list(_RLS_ORDER_COLUMNS),
            "result": result,
        }

    # ------------------------------------------------------------------
    # GET — single RLS filter
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Row Level Security")],
    )
    async def get_single(
        self,
        dao: CRUDDAOProtocol,
        pk: int,
    ) -> dict[str, Any]:
        """Get a Row Level Security filter by id.

        Returns the rule's ``name``, ``description``, ``filter_type``,
        ``clause``, ``group_key``, plus its associated ``tables`` and
        ``roles`` — mirrors ``RLSShowSchema`` from
        ``superset_old/row_level_security/schemas.py``.
        """
        from sqlalchemy import select

        from superset.models.connectors import RowLevelSecurityFilter

        stmt = (
            select(RowLevelSecurityFilter)
            .where(RowLevelSecurityFilter.id == pk)
            .options(
                selectinload(RowLevelSecurityFilter.tables),
                selectinload(RowLevelSecurityFilter.roles),
            )
        )
        result = await dao.session.execute(stmt)
        item = result.scalars().one_or_none()
        if item is None:
            raise ObjectNotFoundError("RowLevelSecurityFilter", pk)
        await event_logger.alog_with_context("rls.show", object_ref=str(pk))
        return {"id": pk, "result": _serialize_rls_show_item(item)}

    # ------------------------------------------------------------------
    # POST — create RLS filter
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "Row Level Security")],
        status_code=201,
    )
    async def create(
        self,
        dao: CRUDDAOProtocol,
        data: RLSPostSchema,
    ) -> dict[str, Any]:
        """Create a new Row Level Security filter.

        Body is validated against :class:`RLSPostSchema`. Returns
        ``{"id": <pk>, "result": <validated payload>}`` — matches
        original Superset which returns the validated request payload
        (not the SQLAlchemy instance) under ``result``. See
        ``superset_old/row_level_security/api.py:202``.
        """
        payload = _msgspec_to_dict(data)
        cmd = CreateRLSRuleCommand(dao=dao, data=payload)
        item = await cmd.execute()
        item_id = getattr(item, "id", None)
        await event_logger.alog_with_context(
            "rls.create",
            object_ref=str(item_id) if item_id is not None else None,
        )
        return {"id": item_id, "result": payload}

    # ------------------------------------------------------------------
    # PUT — update RLS filter
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "Row Level Security")],
        status_code=200,
    )
    async def update(
        self,
        dao: CRUDDAOProtocol,
        pk: int,
        data: RLSPutSchema,
    ) -> dict[str, Any]:
        """Update an existing Row Level Security filter.

        All fields are optional (partial update); only the keys
        present in the body are applied. Returns ``{"id": <pk>,
        "result": <patched fields>}`` — matches original Superset which
        returns the validated request payload (not the SQLAlchemy
        instance) under ``result``.
        """
        payload = filter_unset(_msgspec_to_dict(data))
        cmd = UpdateRLSRuleCommand(dao=dao, model_id=pk, data=payload)
        await cmd.execute()
        await event_logger.alog_with_context("rls.update", object_ref=str(pk))
        return {"id": pk, "result": payload}

    # ------------------------------------------------------------------
    # DELETE — bulk delete RLS filters (the *only* delete endpoint —
    # original Superset does not expose ``DELETE /{pk}``).
    # ------------------------------------------------------------------
    @delete(
        "/",
        guards=[require_permission("can_write", "Row Level Security")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: CRUDDAOProtocol,
        rison_params: list[int] | dict[str, Any] | None,
    ) -> dict[str, str]:
        """Bulk-delete Row Level Security filters.

        ``q=!(<id>,<id>,...)`` Rison-encoded array of ids. The only
        delete endpoint exposed by this resource — original Superset
        does not expose a per-id ``DELETE /{pk}``.

        Returns 404 (via :class:`RLSRuleNotFoundError`) if any of the
        requested ids does not exist — matches original behaviour.
        """
        ids = extract_ids(rison_params)
        cmd = DeleteRLSRuleCommand(dao=dao, model_ids=ids)
        await cmd.execute()
        await event_logger.alog_with_context(
            "rls.bulk_delete",
            extra={"count": len(ids), "object_refs": [str(i) for i in ids]},
        )
        return {"message": f"Deleted {len(ids)} rules"}

    @get(
        "/_info",
        guards=[require_permission("can_read", "Row Level Security")],
    )
    async def info(self, dao: CRUDDAOProtocol) -> dict[str, Any]:
        """Get metadata information about this API resource.

        Returns the FAB-style ``permissions``, ``add_columns``,
        ``edit_columns``, ``label_columns``, and ``filters`` payload
        the frontend reads to render the RLS list/edit UIs. Mirrors
        the auto-generated ``/_info`` from
        ``superset_old/row_level_security/api.py``.
        """
        return await get_info_payload(
            dao=dao,
            model_name="RowLevelSecurityFilter",
            permissions=["can_read", "can_write"],
        )

    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Row Level Security")],
    )
    async def related(
        self,
        column_name: str,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Get related-field options for the RLS edit form.

        Used to populate the ``tables``, ``roles``, ``created_by``,
        and ``changed_by`` selects. Mirrors
        ``allowed_rel_fields = {"tables", "roles", "created_by",
        "changed_by"}`` from
        ``superset_old/row_level_security/api.py``.
        """
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"tables", "roles", "created_by", "changed_by"}),
        )
