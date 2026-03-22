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
from litestar.di import Provide

from liteset.commands.css_template import (
    BulkDeleteCssTemplateCommand,
    CreateCssTemplateCommand,
    DeleteCssTemplateCommand,
    UpdateCssTemplateCommand,
)
from liteset.controllers.base import (
    extract_ids_required,
    extract_pagination,
    serialize_list_response,
)
from liteset.events import event_logger
from liteset.exceptions import ObjectNotFoundError
from liteset.guards.rbac import require_permission
from liteset.params.rison import provide_rison_query
from liteset.providers import provide_css_template_dao
from liteset.schemas.css_template import (
    CssTemplatePostSchema,
    CssTemplatePutSchema,
    CssTemplateResponseSchema,
)
from liteset.typing import CRUDDAOProtocol, UserProtocol
from liteset.utils import filter_unset


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
        page, page_size = extract_pagination(rison_params)
        templates = await dao.find_all(page=page, page_size=page_size)
        total = await dao.count()
        event_logger.log("css_template.list")
        return serialize_list_response(
            templates, total, ["id", "template_name", "css", "created_on", "changed_on"]
        )

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "CssTemplate")],
    )
    async def get_css_template(
        self,
        pk: int,
        dao: CRUDDAOProtocol,
    ) -> CssTemplateResponseSchema:
        """GET /api/v1/css_template/<pk> — get a single CSS template."""
        template = await dao.find_by_id(pk)
        if not template:
            raise ObjectNotFoundError("CssTemplate", pk)
        return CssTemplateResponseSchema(
            id=template.id,
            template_name=template.template_name,
            css=template.css,
            created_on=str(getattr(template, "created_on", "")),
            changed_on=str(getattr(template, "changed_on", "")),
        )

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
    ) -> CssTemplateResponseSchema:
        """POST /api/v1/css_template/ — create a CSS template."""
        cmd = CreateCssTemplateCommand(
            dao=dao,
            data={
                "template_name": data.template_name,
                "css": data.css,
            },
        )
        template = await cmd.execute()
        event_logger.log(
            "css_template.create",
            object_ref=f"css_template:{template.id}",
            user_id=current_user.id,
        )
        return CssTemplateResponseSchema(
            id=template.id,
            template_name=template.template_name,
            css=template.css,
            created_on=str(getattr(template, "created_on", "")),
            changed_on=str(getattr(template, "changed_on", "")),
        )

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
    ) -> CssTemplateResponseSchema:
        """PUT /api/v1/css_template/<pk> — update a CSS template."""
        update_data = filter_unset(
            {
                "template_name": data.template_name,
                "css": data.css,
            }
        )
        cmd = UpdateCssTemplateCommand(dao=dao, pk=pk, data=update_data)
        template = await cmd.execute()
        event_logger.log(
            "css_template.update",
            object_ref=f"css_template:{pk}",
            user_id=current_user.id,
        )
        return CssTemplateResponseSchema(
            id=template.id,
            template_name=template.template_name,
            css=template.css,
            created_on=str(getattr(template, "created_on", "")),
            changed_on=str(getattr(template, "changed_on", "")),
        )

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
        event_logger.log("css_template.delete", object_ref=f"css_template:{pk}")
        return {"message": "OK"}

    @delete(
        "/",
        guards=[require_permission("can_write", "CssTemplate")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, str]:
        """DELETE /api/v1/css_template/?q=(ids:!(...)) — bulk delete CSS templates."""
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteCssTemplateCommand(dao=dao, ids=ids)
        await cmd.execute()
        event_logger.log("css_template.bulk_delete", extra={"count": len(ids)})
        return {"message": "OK"}
