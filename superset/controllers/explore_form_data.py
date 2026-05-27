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
"""Explore form data controller — temporary cache for explore form state.

The frontend sends ``{datasource_id, datasource_type, form_data, chart_id?}``
with ``tab_id`` as a query parameter.  The original Superset stores the
serialized value in a KV table keyed by a UUID.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.di import Provide
from litestar.params import Parameter

from superset.commands.explore_form_data.utils import check_access
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import (
    require_authentication,
    require_permission,
)
from superset.providers import (
    provide_chart_dao,
    provide_dataset_dao,
    provide_kv_dao,
    provide_query_dao,
)
from superset.typing import (
    ChartDAOProtocol,
    DatasetDAOProtocol,
    KeyValueDAOProtocol,
    QueryDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)

DatasourceType = Literal["table", "dataset", "query", "saved_query", "view"]


def _validate_form_data_json(form_data: str | None) -> None:
    """Validate that ``form_data`` is well-formed JSON.

    1:1 with the ``validate()`` hooks of the original
    ``CreateFormDataCommand`` / ``UpdateFormDataCommand``
    (``superset_old/commands/explore/form_data/{create,update}.py``):
    ``validate_json(form_data)`` is run whenever ``form_data`` is truthy, and a
    failure surfaces as a marshmallow ``ValidationError`` → HTTP 400. Here we
    use ``superset.utils.json.validate_json`` (which raises ``JSONDecodeError``)
    and re-raise it as a 400 with the original "JSON not valid" message.
    """
    from superset.exceptions import SupersetGenericErrorException
    from superset.utils.json import JSONDecodeError, validate_json

    if form_data:
        try:
            validate_json(form_data)
        except JSONDecodeError as ex:
            raise SupersetGenericErrorException(
                "JSON not valid", status=400
            ) from ex


class FormDataPostSchema(msgspec.Struct):
    """POST body matching the original Superset explore form_data API."""

    datasource_id: int
    datasource_type: DatasourceType
    form_data: str
    chart_id: int | None = None


class FormDataPutSchema(msgspec.Struct):
    """PUT body matching the original Superset explore form_data API."""

    datasource_id: int
    datasource_type: DatasourceType
    form_data: str
    chart_id: int | None = None


class ExploreFormDataController(Controller):
    path = "/api/v1/explore/form_data"
    tags = ["Explore Form Data"]
    resource = "explore_form_data"
    dependencies = {
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
        "chart_dao": Provide(provide_chart_dao, sync_to_thread=False),
        "dataset_dao": Provide(provide_dataset_dao, sync_to_thread=False),
        "query_dao": Provide(provide_query_dao, sync_to_thread=False),
    }

    @get("/{key:str}", guards=[require_authentication])
    async def get_value(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /{key} — retrieve cached form_data.

        1:1 with original GetFormDataCommand: reads stored envelope,
        calls check_access(datasource_id, chart_id, datasource_type) to
        ensure the requesting user still has access to the underlying
        datasource and (optionally) the chart before returning the payload.
        """
        raw = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if raw is None:
            raise ObjectNotFoundError(self.resource, key)

        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            entry = {}

        if isinstance(entry, dict):
            datasource_id: int = entry.get("datasource_id") or 0
            datasource_type: str = entry.get("datasource_type") or "table"
            chart_id: int | None = entry.get("chart_id")
            # 1:1 with superset_old/commands/explore/form_data/get.py:48-53 —
            # ``check_access`` is invoked unconditionally whenever state exists.
            # The original lets ``check_datasource_access`` itself reject a falsy
            # ``datasource_id`` (``DatasourceNotFoundValidationError``); do not
            # short-circuit on ``datasource_id == 0`` here.
            await check_access(
                datasource_id=datasource_id,
                chart_id=chart_id,
                datasource_type=datasource_type,
                dataset_dao=dataset_dao,
                query_dao=query_dao,
                chart_dao=chart_dao,
                security_manager=security_manager,
                user=current_user,
            )
            if "value" in entry:
                return {"form_data": entry["value"]}

        return {"form_data": raw}

    @post(
        "/",
        status_code=201,
        # 1:1 with superset_old/explore/form_data/api.py:50-51 — ``@protect()``
        # returns 401 (not 403) for anonymous callers. ``require_permission``
        # raises ``NotAuthorizedException`` (401) for unauthenticated users that
        # lack the Public-role permission, so we drop the ``deny_anon_with_403``
        # guard which would otherwise 403 anonymous POSTs.
        guards=[
            require_permission("can_write", "ExploreFormDataRestApi"),
        ],
    )
    async def create_value(
        self,
        data: FormDataPostSchema,
        kv_dao: KeyValueDAOProtocol,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        tab_id: int | None = Parameter(query="tab_id", default=None, required=False),
    ) -> dict[str, str]:
        """POST / — create new cached form_data.

        1:1 with original CreateFormDataCommand: validates that ``form_data`` is
        valid JSON (``validate()`` hook) and calls check_access before storing
        so that users cannot cache form data for datasources they cannot access.
        """
        _validate_form_data_json(data.form_data)
        await check_access(
            datasource_id=data.datasource_id,
            chart_id=data.chart_id,
            datasource_type=data.datasource_type,
            dataset_dao=dataset_dao,
            query_dao=query_dao,
            chart_dao=chart_dao,
            security_manager=security_manager,
            user=current_user,
        )
        key = str(uuid.uuid4())
        envelope = json.dumps(
            {
                "owner": current_user.id,
                "datasource_id": data.datasource_id,
                "datasource_type": data.datasource_type,
                "chart_id": data.chart_id,
                "tab_id": tab_id,
                "value": data.form_data,
            }
        )
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=key,
            value=envelope,
        )
        await event_logger.alog_with_context(
            "explore_form_data.create",
            user_id=current_user.id,
        )
        return {"key": key}

    @put("/{key:str}", guards=[require_authentication])
    async def update_value(
        self,
        key: str,
        data: FormDataPutSchema,
        kv_dao: KeyValueDAOProtocol,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        tab_id: int | None = Parameter(query="tab_id", default=None, required=False),
    ) -> dict[str, str]:
        """PUT /{key} — update cached form_data.

        1:1 with original UpdateFormDataCommand: validates that ``form_data`` is
        valid JSON (``validate()`` hook), calls check_access for the new
        datasource/chart being saved, then checks that the requesting user is
        the owner of the entry before allowing the update.
        """
        from litestar.exceptions import PermissionDeniedException

        _validate_form_data_json(data.form_data)

        existing = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if existing is None:
            raise ObjectNotFoundError(self.resource, key)

        # Datasource + chart access check — mirrors UpdateFormDataCommand.
        await check_access(
            datasource_id=data.datasource_id,
            chart_id=data.chart_id,
            datasource_type=data.datasource_type,
            dataset_dao=dataset_dao,
            query_dao=query_dao,
            chart_dao=chart_dao,
            security_manager=security_manager,
            user=current_user,
        )

        # Owner check — mirrors UpdateFormDataCommand.update() in the original.
        try:
            entry = json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            entry = {}
        owner = entry.get("owner")
        if owner is not None and owner != current_user.id:
            raise PermissionDeniedException(
                detail="You don't have access to this resource"
            )

        envelope = json.dumps(
            {
                "owner": current_user.id,
                "datasource_id": data.datasource_id,
                "datasource_type": data.datasource_type,
                "chart_id": data.chart_id,
                "tab_id": tab_id,
                "value": data.form_data,
            }
        )
        await kv_dao.set_value(
            resource=self.resource,
            resource_id=0,
            key=key,
            value=envelope,
        )
        await event_logger.alog_with_context(
            "explore_form_data.update",
            user_id=current_user.id,
        )
        return {"key": key}

    @delete("/{key:str}", status_code=200, guards=[require_authentication])
    async def delete_value(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        query_dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """DELETE /{key} — delete cached form_data.

        1:1 with original DeleteFormDataCommand: reads the stored envelope
        first to extract datasource/chart metadata, calls check_access,
        then verifies ownership before deleting.
        """
        from litestar.exceptions import PermissionDeniedException

        raw = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if raw is None:
            raise ObjectNotFoundError(self.resource, key)

        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            entry = {}

        if isinstance(entry, dict):
            datasource_id: int = entry.get("datasource_id") or 0
            datasource_type: str = entry.get("datasource_type") or "table"
            chart_id: int | None = entry.get("chart_id")
            # 1:1 with superset_old/commands/explore/form_data/delete.py:49-53 —
            # ``check_access`` runs unconditionally when state exists; the falsy
            # ``datasource_id`` case is handled inside ``check_datasource_access``.
            await check_access(
                datasource_id=datasource_id,
                chart_id=chart_id,
                datasource_type=datasource_type,
                dataset_dao=dataset_dao,
                query_dao=query_dao,
                chart_dao=chart_dao,
                security_manager=security_manager,
                user=current_user,
            )
            # Owner check — original raises TemporaryCacheAccessDeniedError
            # when state["owner"] != get_user_id().
            owner = entry.get("owner")
            if owner is not None and owner != current_user.id:
                raise PermissionDeniedException(
                    detail="You don't have access to this resource"
                )

        deleted = await kv_dao.delete_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if not deleted:
            raise ObjectNotFoundError(self.resource, key)
        await event_logger.alog_with_context(
            "explore_form_data.delete",
            user_id=current_user.id,
        )
        return {"message": "OK"}
