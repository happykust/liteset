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
"""Rison query parameter decoder for Superset API compatibility."""

from __future__ import annotations

from typing import Any

import prison
from litestar.connection import Request


async def provide_rison_query(request: Request[Any, Any, Any]) -> dict[str, Any] | None:
    """Decode Rison-encoded 'q' query parameter.

    Frontend sends: GET /api/v1/chart/?q=(filters:!(...),page:0,page_size:25)
    Returns None if 'q' absent. Raises 422 on parse error.
    """
    raw = request.query_params.get("q")
    if raw is None:
        return None
    try:
        return prison.loads(raw)
    except Exception as ex:
        from liteset.exceptions import LitesetValidationException

        raise LitesetValidationException(
            message=f"Invalid Rison query parameter: {ex}",
            extra={"raw_value": raw},
        ) from ex
