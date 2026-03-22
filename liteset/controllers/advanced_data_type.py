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

from liteset.exceptions import LitesetValidationException
from liteset.schemas.advanced_data_type import (
    AdvancedDataTypeConvertRequest,
    AdvancedDataTypeConvertResponse,
)


class AdvancedDataTypeController(Controller):
    path = "/api/v1/advanced_data_type"
    tags = ["Advanced Data Type"]

    @get("/types")
    async def get_types(self, state: State) -> dict[str, list[str]]:
        """GET /api/v1/advanced_data_type/types — list registered advanced data types."""
        registry: dict[str, Any] = getattr(
            state.settings, "advanced_data_types", {}
        )
        return {"result": list(registry.keys())}

    @post("/convert")
    async def convert(
        self,
        data: AdvancedDataTypeConvertRequest,
        state: State,
    ) -> dict[str, list[dict[str, Any]]]:
        """POST /api/v1/advanced_data_type/convert — convert values to advanced type."""
        registry: dict[str, Any] = getattr(
            state.settings, "advanced_data_types", {}
        )
        handler = registry.get(data.type)
        if handler is None:
            raise LitesetValidationException(
                f"Unknown advanced data type: {data.type}"
            )

        if callable(handler):
            result = handler(data.values)
        elif hasattr(handler, "fetch_data"):
            result = handler.fetch_data(data.values)
        else:
            raise LitesetValidationException(
                f"Advanced data type handler for '{data.type}' is not callable"
            )

        return {"result": result}
