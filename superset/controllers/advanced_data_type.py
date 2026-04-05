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

from superset.exceptions import SupersetValidationException
from superset.guards.rbac import require_authentication
from superset.params.rison import provide_rison_query
from superset.schemas.advanced_data_type import (
    AdvancedDataTypeConvertRequest,
)


class AdvancedDataTypeController(Controller):
    path = "/api/v1/advanced_data_type"
    tags = ["Advanced Data Type"]
    dependencies = {
        "rison_params": Provide(provide_rison_query),
    }

    @get("/types", guards=[require_authentication])
    async def get_types(self, state: State) -> dict[str, list[str]]:
        """GET /api/v1/advanced_data_type/types
        -- list registered advanced data types.
        """
        registry: dict[str, Any] = getattr(state.settings, "advanced_data_types", {})
        return {"result": list(registry.keys())}

    @post("/convert", guards=[require_authentication])
    async def convert(
        self,
        data: AdvancedDataTypeConvertRequest,
        state: State,
    ) -> dict[str, list[dict[str, Any]]]:
        """POST /api/v1/advanced_data_type/convert -- convert values."""
        registry: dict[str, Any] = getattr(state.settings, "advanced_data_types", {})
        handler = registry.get(data.type)
        if handler is None:
            raise SupersetValidationException(
                f"Unknown advanced data type: {data.type}"
            )

        if callable(handler):
            result = handler(data.values)
        elif hasattr(handler, "fetch_data"):
            result = handler.fetch_data(data.values)
        else:
            raise SupersetValidationException(
                f"Advanced data type handler for '{data.type}' is not callable"
            )

        return {"result": result}

    @get("/convert", guards=[require_authentication])
    async def convert_get(
        self,
        rison_params: dict[str, Any] | None,
        state: State,
    ) -> dict[str, list[dict[str, Any]]]:
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

        registry: dict[str, Any] = getattr(state.settings, "advanced_data_types", {})
        handler = registry.get(adv_type)
        if handler is None:
            raise SupersetValidationException(f"Unknown advanced data type: {adv_type}")

        if callable(handler):
            result = handler(values)
        elif hasattr(handler, "fetch_data"):
            result = handler.fetch_data(values)
        else:
            raise SupersetValidationException(
                f"Advanced data type handler for '{adv_type}' is not callable"
            )

        return {"result": result}
