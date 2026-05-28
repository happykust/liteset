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


async def provide_rison_query(
    request: Request[Any, Any, Any],
) -> dict[str, Any] | list[Any] | None:
    """Decode Rison-encoded 'q' query parameter.

    Frontend sends ``?q=(filters:!(...),page:0,page_size:25)`` (object) for
    list endpoints, but a handful of handlers (e.g. ``/favorite_status/``)
    receive ``?q=!(1,2,3)`` (list). Returns None if 'q' absent. Raises 422
    on parse error or on top-level scalar.
    """
    raw = request.query_params.get("q")
    if raw is None:
        return None
    from superset.exceptions import SupersetValidationException

    try:
        parsed = prison.loads(raw)
    except Exception as ex:
        raise SupersetValidationException(
            message=f"Invalid Rison query parameter: {ex}",
            extra={"raw_value": raw[:200] if raw else ""},
        ) from ex
    # ``prison.loads`` accepts top-level scalars too (``"abc"`` parses to the
    # bare string ``"abc"``), which would later AttributeError on ``.get(...)``
    # → 500. Lists are LEGAL for some handlers (e.g. /chart/favorite_status/
    # takes ``q=!(1,2,3)``, typed ``list[int] | dict[str, Any] | None``), so
    # only reject scalars; let handlers validate the shape they actually need.
    if not isinstance(parsed, (dict, list)):
        raise SupersetValidationException(
            message=(
                "Invalid Rison query parameter: expected an object or list, "
                f"got {type(parsed).__name__}"
            ),
            extra={"raw_value": raw[:200] if raw else ""},
        )
    return parsed
