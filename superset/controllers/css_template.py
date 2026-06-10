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
"""CSS Template controller — CRUD + bulk delete for CSS templates."""

from __future__ import annotations

from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.datastructures import State
from litestar.di import Provide

from superset.commands.css_template import (
    BulkDeleteCssTemplateCommand,
    CreateCssTemplateCommand,
    DeleteCssTemplateCommand,
    UpdateCssTemplateCommand,
)
from superset.controllers.base import (
    build_rison_query_params,
    extract_ids_required,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.i18n import gettext as _
from superset.params.rison import provide_rison_query
from superset.providers import provide_css_template_dao
from superset.schemas.css_template import (
    CssTemplatePostSchema,
    CssTemplatePutSchema,
)
from superset.typing import CRUDDAOProtocol, SecurityManagerProtocol, UserProtocol
from superset.utils import filter_unset


class CssTemplateController(Controller):
    path = "/api/v1/css_template"
    tags = ["CSS Templates"]
    dependencies = {
        "dao": Provide(provide_css_template_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "CssTemplate")],
    )
    async def get_list(
        self,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/css_template/ — list CSS templates with optional pagination."""
        from sqlalchemy import or_
        from sqlalchemy.orm import selectinload

        from superset.models.core import CssTemplate

        def _css_template_all_text(model: Any, value: Any) -> Any:
            """``CssTemplateAllTextFilter`` — free-text over template_name + css
            (1:1 upstream)."""
            if not value:
                return None
            ilike = f"%{value}%"
            return or_(model.template_name.ilike(ilike), model.css.ilike(ilike))

        rison_filters, order_by, page, page_size = build_rison_query_params(
            CssTemplate,
            rison_params,
            custom_filters={"css_template_all_text": _css_template_all_text},
        )
        templates = await dao.find_all(
            filters=rison_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(CssTemplate.changed_by),
                selectinload(CssTemplate.created_by),
            ],
        )
        total = await dao.count(filters=rison_filters or None)
        await event_logger.alog_with_context("css_template.list")
        return serialize_list_response(
            templates,
            total,
            [
                "id",
                "template_name",
                "css",
                "created_on",
                "changed_on_delta_humanized",
                "changed_by.first_name",
                "changed_by.id",
                "changed_by.last_name",
                "created_by.first_name",
                "created_by.id",
                "created_by.last_name",
            ],
            list_title="List Css Template",
            order_columns=["template_name"],
        )

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "CssTemplate")],
    )
    async def get_css_template(
        self,
        pk: int,
        dao: CRUDDAOProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/css_template/<pk> — get a single CSS template.

        Mirrors the FAB ``get_headless`` envelope ``{"id": <pk>, "result":
        {...}}`` (the original ``CssTemplateRestApi`` uses the standard FAB
        ``get``). ``result`` carries exactly the original ``show_columns``:
        id / template_name / css / changed_on_delta_humanized + nested
        changed_by / created_by — NOT the full timestamps (which aren't in
        ``show_columns``).
        """
        from sqlalchemy.orm import selectinload

        from superset.models.core import CssTemplate

        # Eager-load changed_by/created_by so the response serialization below
        # reads them without a sync lazy-load (MissingGreenlet) on the async
        # session — ``find_by_id`` alone returns a bare row (the list endpoint
        # already eager-loads these).
        results = await dao.find_all(
            filters=[CssTemplate.id == pk],
            options=[
                selectinload(CssTemplate.changed_by),
                selectinload(CssTemplate.created_by),
            ],
        )
        template = results[0] if results else None
        if not template:
            raise ObjectNotFoundError("CssTemplate", pk)

        def _user_ref(user: Any) -> dict[str, Any] | None:
            if not user:
                return None
            return {
                "id": user.id,
                "first_name": getattr(user, "first_name", ""),
                "last_name": getattr(user, "last_name", ""),
            }

        return {
            "id": template.id,
            "result": {
                "id": template.id,
                "template_name": template.template_name,
                "css": template.css,
                "changed_on_delta_humanized": getattr(
                    template, "changed_on_delta_humanized", None
                ),
                "changed_by": _user_ref(getattr(template, "changed_by", None)),
                "created_by": _user_ref(getattr(template, "created_by", None)),
            },
        }

    @post(
        "/",
        guards=[require_permission("can_write", "CssTemplate")],
        status_code=201,
    )
    async def create(
        self,
        data: CssTemplatePostSchema,
        dao: CRUDDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """POST /api/v1/css_template/ — create a CSS template.

        Mirrors FAB ``post_headless`` envelope:
        ``{"id": <pk>, "result": <add_columns dump of submitted fields>}``.
        """
        cmd = CreateCssTemplateCommand(
            dao=dao,
            data={
                "template_name": data.template_name,
                "css": data.css,
            },
        )
        template = await cmd.execute()
        await event_logger.alog_with_context(
            "css_template.create",
            object_ref=f"css_template:{template.id}",
            user_id=current_user.id,
        )
        return {
            "id": template.id,
            "result": {
                "template_name": data.template_name,
                "css": data.css,
            },
        }

    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "CssTemplate")],
    )
    async def update(
        self,
        pk: int,
        data: CssTemplatePutSchema,
        dao: CRUDDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """PUT /api/v1/css_template/<pk> — update a CSS template.

        Mirrors FAB ``put_headless`` envelope: ``{"result": <edit_columns
        dump of the merged item>}`` (no top-level ``id``). FAB merges the
        submitted fields onto the existing record (PATCH semantics) and
        dumps the full ``edit_columns`` set, so the result reflects the
        persisted values for every editable column.
        """
        update_data = filter_unset(
            {
                "template_name": data.template_name,
                "css": data.css,
            }
        )
        cmd = UpdateCssTemplateCommand(dao=dao, pk=pk, data=update_data)
        template = await cmd.execute()
        await event_logger.alog_with_context(
            "css_template.update",
            object_ref=f"css_template:{pk}",
            user_id=current_user.id,
        )
        return {
            "result": {
                "template_name": template.template_name,
                "css": template.css,
            },
        }

    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "CssTemplate")],
        status_code=200,
    )
    async def delete_css_template(
        self,
        pk: int,
        dao: CRUDDAOProtocol,
    ) -> dict[str, str]:
        """DELETE /api/v1/css_template/<pk> — delete a single CSS template."""
        cmd = DeleteCssTemplateCommand(dao=dao, pk=pk)
        await cmd.execute()
        await event_logger.alog_with_context(
            "css_template.delete", object_ref=f"css_template:{pk}"
        )
        return {"message": "OK"}

    @delete(
        "/",
        guards=[require_permission("can_write", "CssTemplate")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: CRUDDAOProtocol,
        rison_params: list[int] | dict[str, Any] | None,
    ) -> dict[str, str]:
        """DELETE /api/v1/css_template/?q=(ids:!(...)) — bulk delete CSS templates."""
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteCssTemplateCommand(dao=dao, ids=ids)
        await cmd.execute()
        await event_logger.alog_with_context(
            "css_template.bulk_delete", extra={"count": len(ids)}
        )
        num = len(ids)
        message = (
            _("Deleted %(num)d css template", num=num)
            if num == 1
            else _("Deleted %(num)d css templates", num=num)
        )
        return {"message": message}

    @get(
        "/_info",
        guards=[require_permission("can_read", "CssTemplate")],
    )
    async def info(
        self,
        dao: CRUDDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/css_template/_info -- API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="CssTemplate",
            permissions=["can_read", "can_write"],
            security_manager=security_manager,
            current_user=current_user,
            class_permission_name="CssTemplate",
        )

    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "CssTemplate")],
    )
    async def related(
        self,
        column_name: str,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
        state: State,
        security_manager: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/css_template/related/{column_name}.

        Mirrors ``CssTemplateRestApi.base_related_field_filters`` which applies
        ``BaseFilterRelatedUsers`` (superset_old/views/filters.py:56-87) on the
        ``changed_by`` field, excluding users in ``EXCLUDE_USERS_FROM_LISTS`` and
        applying the ``EXTRA_RELATED_QUERY_FILTERS["user"]`` hook.
        """
        allowed_rel_fields = frozenset({"created_by", "changed_by"})

        # Apply BaseFilterRelatedUsers logic for changed_by only
        # (original base_related_field_filters only covers changed_by).
        base_filters: list[Any] = []
        query_hook: Any | None = None
        if column_name == "changed_by":
            from superset.models.security import User

            settings = getattr(state, "settings", None)

            # Step 1: Apply EXTRA_RELATED_QUERY_FILTERS["user"] hook.
            # Original contract (superset_old/views/filters.py:72-76):
            #   query = extra_filters(query)  — Callable[[Query], Query]
            # We pass the hook through to get_related_payload as query_hook
            # so it receives the real Select statement and returns the
            # modified Select, matching the original calling convention.
            # Original BaseFilterRelatedUsers.apply() has no try/except —
            # any exception propagates as HTTP 500 (mirrors original behaviour).
            extra_related_filters: dict[str, Any] = (
                getattr(settings, "extra_related_query_filters", {}) if settings else {}
            )
            user_extra_filter = extra_related_filters.get("user")
            if callable(user_extra_filter):
                query_hook = user_extra_filter

            # Step 2: Determine exclude_users list with fallback
            # Original: EXCLUDE_USERS_FROM_LISTS is None -> call
            # security_manager.get_exclude_users_from_lists()
            exclude_users: list[str] | None = (
                getattr(settings, "exclude_users_from_lists", None)
                if settings
                else None
            )
            if exclude_users is None:
                get_exclude = getattr(
                    security_manager, "get_exclude_users_from_lists", None
                )
                if callable(get_exclude):
                    exclude_users = get_exclude()

            # Step 3: Exclude matched usernames
            if exclude_users:
                base_filters.append(User.username.not_in(exclude_users))

        # Original suppresses text filter for 'created_by' because it has no
        # entry in related_field_filters
        # (superset_old/css_templates/api.py:99-101 covers changed_by only).
        if column_name == "created_by" and rison_params and "filter" in rison_params:
            rison_params = dict(rison_params)
            rison_params.pop("filter")

        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=allowed_rel_fields,
            base_filters=base_filters if base_filters else None,
            query_hook=query_hook,
        )
