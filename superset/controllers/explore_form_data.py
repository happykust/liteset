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
"""Explore form data controller — temporary cache for explore form state."""

from __future__ import annotations

import json
from typing import Any

from litestar import get
from litestar.di import Provide

from superset.controllers.temporary_cache import TemporaryCacheController
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_authentication
from superset.providers import provide_kv_dao
from superset.typing import KeyValueDAOProtocol, UserProtocol


class ExploreFormDataController(TemporaryCacheController):
    path = "/api/v1/explore/form_data"
    tags = ["Explore Form Data"]
    resource = "explore_form_data"
    dependencies = {
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
    }

    @get("/{key:str}", guards=[require_authentication])
    async def get_value(
        self,
        key: str,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /{key} -- retrieve cached form_data.

        Frontend expects ``{"form_data": "..."}`` for explore form data,
        unlike dashboard_filter_state which uses ``{"value": "..."}``.
        """
        raw = await kv_dao.get_value(
            resource=self.resource,
            resource_id=0,
            key=key,
        )
        if raw is None:
            raise ObjectNotFoundError(self.resource, key)

        # Unwrap envelope
        try:
            entry = json.loads(raw)
            if isinstance(entry, dict) and "value" in entry:
                return {"form_data": entry["value"]}
        except (json.JSONDecodeError, TypeError):
            pass
        return {"form_data": raw}
