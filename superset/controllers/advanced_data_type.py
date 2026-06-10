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
"""Advanced data type controller — type registry and value conversion."""

from __future__ import annotations

from typing import Any, cast

from litestar import Controller, get, post
from litestar.datastructures import State
from litestar.di import Provide
from litestar.response import Response

from superset.advanced_data_type.types import AdvancedDataType
from superset.events import event_logger
from superset.exceptions import SupersetValidationException
from superset.guards.rbac import require_permission
from superset.i18n import gettext as _
from superset.params.rison import provide_rison_query
from superset.schemas.advanced_data_type import (
    AdvancedDataTypeConvertRequest,
)


def _get_registry(state: State) -> dict[str, Any]:
    """Return the advanced_data_types registry from config.

    Mirrors the original ``app.config['ADVANCED_DATA_TYPES']`` read in
    superset_old/advanced_data_type/api.py:96,148 — whatever the user
    configured is returned as-is.  The built-in defaults
    (``internet_address`` + ``port``) live in the field default in
    ``superset/config.py``, not here.  Injecting them unconditionally
    here would override a user's explicit ``ADVANCED_DATA_TYPES = {}``
    with the built-ins, diverging from original behaviour.
    """
    return getattr(state.settings, "advanced_data_types", {}) or {}


def _invoke_handler(handler: Any, adv_type: str, values: list[Any]) -> Any:
    """Invoke an advanced-data-type handler.

    Supports three handler shapes:
    - ``AdvancedDataType`` dataclass with ``translate_type`` callable
      (the canonical plugin shape — matches original)
    - bare callable accepting the values list
    - object exposing ``fetch_data(values)``
    """
    if isinstance(handler, AdvancedDataType):
        # 1:1 with the original call shape (superset_old/advanced_data_type/
        # api.py:105-109): the request dict carries ONLY ``values`` — no
        # extra ``advanced_data_type`` key that third-party plugins never saw.
        return handler.translate_type({"values": values})  # type: ignore[typeddict-item]
    if callable(handler):
        return handler(values)
    if hasattr(handler, "fetch_data"):
        return handler.fetch_data(values)
    raise SupersetValidationException(
        f"Advanced data type handler for '{adv_type}' is not callable"
    )


class AdvancedDataTypeController(Controller):
    path = "/api/v1/advanced_data_type"
    tags = ["Advanced Data Type"]
    dependencies = {
        "rison_params": Provide(provide_rison_query),
    }

    @get("/types", guards=[require_permission("can_read", "AdvancedDataType")])
    async def get_types(self, state: State) -> dict[str, list[str]]:
        """GET /api/v1/advanced_data_type/types -- list registered types."""
        await event_logger.alog_with_context(
            action="AdvancedDataTypeRestApi.get",
            log_to_statsd=False,
        )
        registry = _get_registry(state)
        return {"result": list(registry.keys())}

    @post(
        "/convert",
        guards=[require_permission("can_read", "AdvancedDataType")],
        # Upstream only ships GET /convert (advanced_data_type/api.py:53);
        # this POST variant exists for clients that prefer a body over a
        # rison query string. Either way it returns the conversion result
        # — no resource is created — so 200, not Litestar's default 201.
        status_code=200,
    )
    async def convert(
        self,
        data: AdvancedDataTypeConvertRequest,
        state: State,
    ) -> dict[str, Any] | Response[dict[str, Any]]:
        """POST /api/v1/advanced_data_type/convert -- convert values."""
        await event_logger.alog_with_context(
            action="AdvancedDataTypeRestApi.get",
            log_to_statsd=False,
        )
        registry = _get_registry(state)
        handler = registry.get(data.type)
        if not handler:
            # Mirror superset_old/advanced_data_type/api.py ``get``:
            # HTTP 400 "Invalid advanced data type: <type>".
            return Response(
                content={
                    "message": _(
                        "Invalid advanced data type: %(advanced_data_type)s",
                        advanced_data_type=data.type,
                    )
                },
                status_code=400,
            )
        result = _invoke_handler(handler, data.type, data.values)
        return {"result": result}

    @get("/convert", guards=[require_permission("can_read", "AdvancedDataType")])
    async def convert_get(
        self,
        rison_params: dict[str, Any] | None,
        state: State,
    ) -> dict[str, Any] | Response[dict[str, Any]]:
        """GET /api/v1/advanced_data_type/convert -- convert via RISON params.

        Accepts ``type`` and ``values`` from the Rison query parameter,
        matching the original Flask API signature.  The original uses
        ``@rison(advanced_data_type_convert_schema)`` which validates
        against a JSON Schema requiring both ``type`` (string) and
        ``values`` (array, minItems: 1).
        """
        await event_logger.alog_with_context(
            action="AdvancedDataTypeRestApi.get",
            log_to_statsd=False,
        )
        params = rison_params or {}
        # The original @rison(advanced_data_type_convert_schema) requires the
        # RISON parameter to be a JSON object ({"type": "object"}).  A list
        # passes provide_rison_query but must be rejected here with 400, just
        # as FAB's jsonschema.validate() returns 400 for a list input.
        if not isinstance(params, dict):
            return Response(
                content={"message": "Not a valid rison schema"},
                status_code=400,
            )

        # Validate required fields matching the original JSON Schema:
        # {"required": ["type", "values"], "properties": {"type": {"type": "string"},
        #  "values": {"type": "array", "minItems": 1}}}
        errors: list[str] = []
        adv_type = params.get("type")
        if adv_type is None or not isinstance(adv_type, str) or not adv_type:
            errors.append("'type' is a required property")
        values = params.get("values")
        if values is None:
            errors.append("'values' is a required property")
        elif not isinstance(values, list):
            errors.append("'values' must be an array")
        elif len(values) < 1:
            errors.append("'values' must contain at least 1 item")
        if errors:
            return Response(
                content={"message": "; ".join(errors)},
                status_code=400,
            )
        # At this point adv_type is a non-empty str and values is a list
        # with at least 1 item.
        adv_type = cast(str, adv_type)
        values = cast(list[Any], values)

        registry = _get_registry(state)
        handler = registry.get(adv_type)
        if not handler:
            # Mirror superset_old/advanced_data_type/api.py ``get``:
            # HTTP 400 "Invalid advanced data type: <type>".
            return Response(
                content={
                    "message": _(
                        "Invalid advanced data type: %(advanced_data_type)s",
                        advanced_data_type=adv_type,
                    )
                },
                status_code=400,
            )
        result = _invoke_handler(handler, adv_type, values)
        return {"result": result}
