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
from __future__ import annotations

import logging

from liteset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec
from liteset.db.engine_specs.clickhouse import AsyncClickHouseEngineSpec
from liteset.db.engine_specs.mysql import AsyncMySQLEngineSpec
from liteset.db.engine_specs.postgres import AsyncPostgresEngineSpec
from liteset.db.engine_specs.sync_fallback import (
    make_async_spec,
    SyncFallbackEngineSpec,
)
from liteset.db.engine_specs.trino import AsyncTrinoEngineSpec

logger = logging.getLogger(__name__)

_NATIVE_SPECS: dict[str, type[BaseAsyncEngineSpec]] = {
    "postgresql": AsyncPostgresEngineSpec,
    "mysql": AsyncMySQLEngineSpec,
    "clickhouse": AsyncClickHouseEngineSpec,
    "trino": AsyncTrinoEngineSpec,
}

# Cache for dynamically created sync fallback specs.
# Race condition on concurrent access is benign — results are idempotent.
_fallback_cache: dict[str, type[SyncFallbackEngineSpec]] = {}

# Cached map of sync engine specs keyed by engine name.
_sync_spec_map: dict[str, type] | None = None


def _get_sync_spec_map() -> dict[str, type]:
    """Build and cache a map of sync engine specs by engine name."""
    global _sync_spec_map
    if _sync_spec_map is None:
        from superset.db_engine_specs import load_engine_specs

        _sync_spec_map = {
            getattr(cls, "engine", ""): cls
            for cls in load_engine_specs()
            if getattr(cls, "engine", "")
        }
    return _sync_spec_map


def get_async_engine_spec(engine: str) -> type[BaseAsyncEngineSpec]:
    """Return an async engine spec for the given engine name.

    Tries native async specs first; falls back to wrapping the sync
    Flask BaseEngineSpec via SyncFallbackEngineSpec.
    """
    if engine in _NATIVE_SPECS:
        return _NATIVE_SPECS[engine]

    if engine in _fallback_cache:
        return _fallback_cache[engine]

    # Attempt to find and wrap a sync spec from superset
    try:
        sync_spec_map = _get_sync_spec_map()
        if engine in sync_spec_map:
            async_spec = make_async_spec(sync_spec_map[engine])
            _fallback_cache[engine] = async_spec
            logger.info("Created sync fallback async spec for engine: %s", engine)
            return async_spec
    except (ImportError, ModuleNotFoundError):
        logger.warning(
            "Could not load superset engine specs for fallback. "
            "Engine '%s' is not supported.",
            engine,
        )

    raise ValueError(f"No async engine spec found for engine: {engine}")


__all__ = [
    "AsyncClickHouseEngineSpec",
    "AsyncMySQLEngineSpec",
    "AsyncPostgresEngineSpec",
    "AsyncTrinoEngineSpec",
    "AsyncResultSet",
    "BaseAsyncEngineSpec",
    "SyncFallbackEngineSpec",
    "get_async_engine_spec",
    "make_async_spec",
]
