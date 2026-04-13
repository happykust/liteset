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
"""Legacy API controller.

Ports still-active endpoints from the original ``superset/views/api.py``
(class ``Api`` extending ``BaseSupersetView``). Currently hosts the
``/api/v1/time_range/`` endpoint that the Explore UI hits every time a user
edits a time range filter.
"""

from __future__ import annotations

import logging
from typing import Any

from litestar import Controller, Request, get
from litestar.response import Response

from superset.exceptions import SupersetValidationException
from superset.guards.rbac import require_authentication
from superset.typing import UserProtocol
from superset.utils.date import (
    TimeRangeAmbiguousError,
    TimeRangeParseFailError,
    get_since_until,
)

logger = logging.getLogger(__name__)


def _parse_rison_time_ranges(raw: str | None) -> Any:
    """Decode the ``q=`` Rison-encoded query parameter used by the original
    ``/api/v1/time_range/`` endpoint. Accepts either a single string or a
    list of ``{timeRange, shift}`` objects.
    """
    if raw is None:
        raise SupersetValidationException(
            message="Missing required query parameter 'q'",
        )
    try:
        import prison

        return prison.loads(raw)
    except Exception as ex:  # noqa: BLE001
        raise SupersetValidationException(
            message=f"Invalid Rison query parameter: {ex}",
            extra={"raw_value": raw[:200]},
        ) from ex


class LegacyApiController(Controller):
    """Legacy ``/api/v1/*`` routes ported from ``superset/views/api.py``."""

    path = "/api/v1"
    tags = ["Legacy"]

    @get("/time_range/", status_code=200, guards=[require_authentication])
    async def time_range(
        self,
        request: Request[Any, Any, Any],
        current_user: UserProtocol,
    ) -> Response[dict[str, Any]]:
        """GET /api/v1/time_range/?q=<rison>

        Return ``since`` / ``until`` datetimes from human-readable ``timeRange``
        expressions (e.g. ``"Last week"``, ``"100 years ago : now"``). The
        ``q`` argument is a Rison-encoded string or list of
        ``{timeRange, shift}`` objects. Response matches the original Flask
        view shape:

            {"result": [{"since": "...", "until": "...", "timeRange": "...",
                          "shift": "..."}, ...]}
        """
        # require_authentication guard enforces a valid user session.
        del current_user  # parameter exists only to trigger the guard
        raw = request.query_params.get("q")
        time_ranges = _parse_rison_time_ranges(raw)

        if isinstance(time_ranges, str):
            time_ranges = [{"timeRange": time_ranges}]
        if not isinstance(time_ranges, list):
            return Response(
                content={
                    "message": "'q' must be a string or list of "
                    "{timeRange, shift} objects",
                },
                status_code=400,
            )

        try:
            rv: list[dict[str, Any]] = []
            for entry in time_ranges:
                if not isinstance(entry, dict) or "timeRange" not in entry:
                    return Response(
                        content={
                            "message": "Each time range entry must be an "
                            "object with a 'timeRange' key",
                        },
                        status_code=400,
                    )
                since, until = get_since_until(
                    time_range=entry["timeRange"],
                    time_shift=entry.get("shift"),
                )
                rv.append(
                    {
                        "since": since.isoformat() if since else "",
                        "until": until.isoformat() if until else "",
                        "timeRange": entry["timeRange"],
                        "shift": entry.get("shift"),
                    }
                )
            return Response(content={"result": rv}, status_code=200)
        except (
            ValueError,
            TimeRangeParseFailError,
            TimeRangeAmbiguousError,
        ) as ex:
            return Response(
                content={"message": f"Unexpected time range: {ex}"},
                status_code=400,
            )
