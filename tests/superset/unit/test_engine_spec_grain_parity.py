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
"""Regression test for R13-09 — time-grain key parity between the sync and
async engine-spec packages.

Two-package topology hazard: the explore UI builds its grain dropdown from
the SYNC spec (``Database.db_engine_spec`` → ``superset.db_engine_specs``,
the upstream copy), while the async package keeps its own grain tables
(``superset.db.engine_specs``), consumed via
``get_engine_spec_for_database`` (today only by the currently-unwired
``SqlaTable._get_time_grain_expr``; any future consumer inherits the same
contract).  Any grain key the sync table offers but the async table lacks
makes such a consumer silently fall back to the un-truncated column —
charts group by raw timestamps instead of the selected grain (this is
exactly how Trino's ``PT0.5H`` half-hour grain was spelled ``PT30M``).
"""

from __future__ import annotations

import pytest


def _native_specs_with_sync_counterpart() -> list[tuple[str, type, type]]:
    from superset.db.engine_specs.__init__ import (
        _get_sync_spec_map,
        _NATIVE_SPECS,
    )

    sync_map = _get_sync_spec_map()
    return [
        (engine, async_spec, sync_map[engine])
        for engine, async_spec in sorted(_NATIVE_SPECS.items())
        if engine in sync_map
    ]


@pytest.mark.parametrize(
    "engine,async_spec,sync_spec",
    _native_specs_with_sync_counterpart(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_async_grain_table_covers_sync_keys(engine, async_spec, sync_spec) -> None:
    """Every grain the (UI-facing) sync spec offers must exist in the async
    spec's table, or the SQL builder silently drops the time truncation."""
    sync_keys = {str(k) for k in sync_spec.get_time_grain_expressions() if k}
    async_keys = {str(k) for k in async_spec.get_time_grain_expressions() if k}
    missing = sync_keys - async_keys
    assert not missing, (
        f"{engine}: async spec is missing grain keys offered by the sync/UI "
        f"spec: {sorted(missing)} — charts using them lose truncation silently"
    )
