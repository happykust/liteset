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
"""Cache invalidation controller."""

from __future__ import annotations

from typing import Any

import msgspec
from litestar import Controller, post

from liteset.events import event_logger
from liteset.guards.rbac import require_permission


class CacheInvalidateSchema(msgspec.Struct):
    """Body for POST /api/v1/cachekey/invalidate."""

    datasource_uids: list[str]


class CacheController(Controller):
    path = "/api/v1/cachekey"
    tags = ["Cache"]

    @post(
        "/invalidate",
        guards=[require_permission("can_write", "CacheKey")],
    )
    async def invalidate(
        self,
        data: CacheInvalidateSchema,
    ) -> dict[str, str]:
        """POST /api/v1/cachekey/invalidate — invalidate cache keys."""
        # Cache invalidation logic would go through CacheKey model.
        # For now, log the invalidation request.
        event_logger.log(
            "cache.invalidate",
            extra={"datasource_uids": data.datasource_uids},
        )
        return {"message": f"Invalidated {len(data.datasource_uids)} cache entries"}
