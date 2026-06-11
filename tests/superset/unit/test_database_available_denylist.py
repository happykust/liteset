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
"""R13-11: GET /database/available/ must honour DBS_AVAILABLE_DENYLIST.

Upstream ``get_available_engine_specs`` skips engines whose
``default_driver`` is listed in ``DBS_AVAILABLE_DENYLIST``; the port
previously ignored the setting entirely.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from superset.controllers.database import DatabaseController

_available = DatabaseController.available
_available_fn = _available.fn if hasattr(_available, "fn") else _available


def _settings(denylist: dict[str, set[str]]) -> MagicMock:
    settings = MagicMock()
    settings.dbs_available_denylist = denylist
    settings.preferred_databases = []
    return settings


async def _call_available(denylist: dict[str, set[str]]) -> list[dict]:
    with patch(
        "superset.controllers.database.SupersetSettings",
        return_value=_settings(denylist),
    ):
        result = await _available_fn(MagicMock())
    return result["databases"]


async def test_denylisted_engine_is_hidden():
    databases = await _call_available({})
    assert databases, "need at least one available engine for this test"
    target = databases[0]
    denylist = {target["engine"]: {target.get("default_driver", "")}}

    remaining = await _call_available(denylist)

    hidden = {(db["engine"], db.get("default_driver", "")) for db in remaining}
    assert (target["engine"], target.get("default_driver", "")) not in hidden
    # Only the denied engine disappears — everything else stays.
    assert len(remaining) == len(databases) - 1


async def test_empty_denylist_changes_nothing():
    baseline = await _call_available({})
    again = await _call_available({})
    assert [d["engine"] for d in baseline] == [d["engine"] for d in again]
