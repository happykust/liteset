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

from typing import Any

from litestar import Controller, get, post
from litestar.datastructures import State
from litestar.di import Provide

from superset.advanced_data_type.types import AdvancedDataType
from superset.exceptions import SupersetValidationException
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.schemas.advanced_data_type import (
    AdvancedDataTypeConvertRequest,
)


def _get_registry(state: State) -> dict[str, Any]:
    """Return the merged advanced_data_types registry with defaults.

    Mirrors original Superset's ``ADVANCED_DATA_TYPES`` config which
    ships ``port`` and ``internet_address`` plugins out of the box (see
    superset_old/config.py:2053-2056).
    """
    from superset.advanced_data_type.plugins.internet_address import internet_address
    from superset.advanced_data_type.plugins.internet_port import internet_port

    user_registry: dict[str, Any] = (
        getattr(state.settings, "advanced_data_types", {}) or {}
    )
    return {"internet_address": internet_address, "port": internet_port, **user_registry}


def _invoke_handler(handler: Any, adv_type: str, values: list[Any]) -> Any:
    """Invoke an advanced-data-type handler.

    Supports three handler shapes:
    - ``AdvancedDataType`` dataclass with ``translate_type`` callable
      (the canonical plugin shape — matches original)
    - bare callable accepting the values list
    - object exposing ``fetch_data(values)``
    """
    if isinstance(handler, AdvancedDataType):
        return handler.translate_type(
            {"advanced_data_type": adv_type, "values": values}
        )
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
        registry = _get_registry(state)
        return {"result": list(registry.keys())}

    @post("/convert", guards=[require_permission("can_read", "AdvancedDataType")])
    async def convert(
        self,
        data: AdvancedDataTypeConvertRequest,
        state: State,
    ) -> dict[str, Any]:
        """POST /api/v1/advanced_data_type/convert -- convert values."""
        registry = _get_registry(state)
        handler = registry.get(data.type)
        if handler is None:
            raise SupersetValidationException(
                f"Unknown advanced data type: {data.type}"
            )
        result = _invoke_handler(handler, data.type, data.values)
        return {"result": result}

    @get("/convert", guards=[require_permission("can_read", "AdvancedDataType")])
    async def convert_get(
        self,
        rison_params: dict[str, Any] | None,
        state: State,
    ) -> dict[str, Any]:
        """GET /api/v1/advanced_data_type/convert -- convert via RISON params.

        Accepts ``type`` and ``values`` from the Rison query parameter,
        matching the original Flask API signature.
        """
        params = rison_params or {}
        adv_type: str = params.get("type", "")
        values: list[str] = params.get("values", [])

        if not adv_type:
            raise SupersetValidationException(
                "'type' is required in the RISON query parameter"
            )

        registry = _get_registry(state)
        handler = registry.get(adv_type)
        if handler is None:
            raise SupersetValidationException(f"Unknown advanced data type: {adv_type}")
        result = _invoke_handler(handler, adv_type, values)
        return {"result": result}
