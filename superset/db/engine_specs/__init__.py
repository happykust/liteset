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

import importlib
import logging
import pkgutil
from pathlib import Path

from superset.db.engine_specs.athena import AsyncAthenaEngineSpec
from superset.db.engine_specs.base import AsyncResultSet, BaseAsyncEngineSpec
from superset.db.engine_specs.clickhouse import AsyncClickHouseEngineSpec
from superset.db.engine_specs.cockroachdb import AsyncCockroachDbEngineSpec
from superset.db.engine_specs.cratedb import AsyncCrateDbEngineSpec
from superset.db.engine_specs.databend import AsyncDatabendEngineSpec
from superset.db.engine_specs.denodo import AsyncDenodoEngineSpec
from superset.db.engine_specs.doris import AsyncDorisEngineSpec
from superset.db.engine_specs.dynamodb import AsyncDynamoDBEngineSpec
from superset.db.engine_specs.elasticsearch import (
    AsyncElasticsearchEngineSpec,
    AsyncOpenDistroEngineSpec,
)
from superset.db.engine_specs.firebird import AsyncFirebirdEngineSpec
from superset.db.engine_specs.firebolt import AsyncFireboltEngineSpec
from superset.db.engine_specs.kusto import (
    AsyncKustoKqlEngineSpec,
    AsyncKustoSqlEngineSpec,
)
from superset.db.engine_specs.mssql import AsyncMSSQLEngineSpec
from superset.db.engine_specs.mysql import AsyncMySQLEngineSpec
from superset.db.engine_specs.oceanbase import AsyncOceanBaseEngineSpec
from superset.db.engine_specs.oracle import AsyncOracleEngineSpec
from superset.db.engine_specs.pinot import AsyncPinotEngineSpec
from superset.db.engine_specs.postgres import AsyncPostgresEngineSpec
from superset.db.engine_specs.redshift import AsyncRedshiftEngineSpec
from superset.db.engine_specs.risingwave import AsyncRisingWaveEngineSpec
from superset.db.engine_specs.starrocks import AsyncStarRocksEngineSpec
from superset.db.engine_specs.sync_fallback import (
    make_async_spec,
    SyncFallbackEngineSpec,
)
from superset.db.engine_specs.trino import AsyncTrinoEngineSpec
from superset.db.engine_specs.ydb import AsyncYDBEngineSpec

logger = logging.getLogger(__name__)

_NATIVE_SPECS: dict[str, type[BaseAsyncEngineSpec]] = {
    # Core 4
    "postgresql": AsyncPostgresEngineSpec,
    "mysql": AsyncMySQLEngineSpec,
    "clickhouse": AsyncClickHouseEngineSpec,
    "trino": AsyncTrinoEngineSpec,
    # PG-wire
    "cockroachdb": AsyncCockroachDbEngineSpec,
    "redshift": AsyncRedshiftEngineSpec,
    "risingwave": AsyncRisingWaveEngineSpec,
    "crate": AsyncCrateDbEngineSpec,
    "denodo": AsyncDenodoEngineSpec,
    # MySQL-wire
    "doris": AsyncDorisEngineSpec,
    "starrocks": AsyncStarRocksEngineSpec,
    "oceanbase": AsyncOceanBaseEngineSpec,
    # Standalone
    "mssql": AsyncMSSQLEngineSpec,
    "oracle": AsyncOracleEngineSpec,
    "firebird": AsyncFirebirdEngineSpec,
    "awsathena": AsyncAthenaEngineSpec,
    "databend": AsyncDatabendEngineSpec,
    "pinot": AsyncPinotEngineSpec,
    "dynamodb": AsyncDynamoDBEngineSpec,
    "elasticsearch": AsyncElasticsearchEngineSpec,
    "odelasticsearch": AsyncOpenDistroEngineSpec,
    "kustosql": AsyncKustoSqlEngineSpec,
    "kustokql": AsyncKustoKqlEngineSpec,
    "yql": AsyncYDBEngineSpec,
    "firebolt": AsyncFireboltEngineSpec,
}

# Cache for dynamically created sync fallback specs.
# Race condition on concurrent access is benign -- results are idempotent.
_fallback_cache: dict[str, type[SyncFallbackEngineSpec]] = {}

# Cached map of sync engine specs keyed by engine name.
_sync_spec_map: dict[str, type] | None = None


def _get_sync_spec_map() -> dict[str, type]:  # noqa: C901
    """Build and cache a map of sync engine specs by engine name.

    Attempts to scan ``superset.db_engine_specs`` for BaseEngineSpec subclasses.
    If the superset package is not installed the function gracefully returns an
    empty dict so that superset can operate independently.
    """
    global _sync_spec_map
    if _sync_spec_map is None:
        _sync_spec_map = {}
        try:
            import superset.db_engine_specs as _pkg  # noqa: F401
            from superset.db_engine_specs.base import BaseEngineSpec  # noqa: F401

            pkg_dir = getattr(_pkg, "__file__", None)
            if pkg_dir is not None:
                pkg_dir = str(Path(pkg_dir).parent)
                for module_info in pkgutil.iter_modules(
                    [pkg_dir], prefix="superset.db_engine_specs."
                ):
                    try:
                        mod = importlib.import_module(module_info.name)
                    except Exception:  # noqa: BLE001, S112
                        continue
                    for attr_name in dir(mod):
                        attr = getattr(mod, attr_name)
                        if (
                            isinstance(attr, type)
                            and issubclass(attr, BaseEngineSpec)
                            and attr is not BaseEngineSpec
                            and getattr(attr, "engine", "")  # type: ignore[arg-type]
                        ):
                            _sync_spec_map[attr.engine] = attr

            # Fallback: try load_engine_specs() if available (e.g. in tests)
            if not _sync_spec_map and hasattr(_pkg, "load_engine_specs"):
                for spec in _pkg.load_engine_specs():
                    engine_name = getattr(spec, "engine", "")
                    if engine_name:
                        _sync_spec_map[engine_name] = spec
        except (ImportError, ModuleNotFoundError):
            logger.debug(
                "superset.db_engine_specs not available; sync fallback disabled"
            )
    return _sync_spec_map


def get_async_engine_spec(engine: str) -> type[BaseAsyncEngineSpec]:
    """Return an async engine spec for the given engine name.

    Tries native async specs first; falls back to wrapping the sync
    Flask BaseEngineSpec via SyncFallbackEngineSpec when superset is
    installed.
    """
    if engine in _NATIVE_SPECS:
        return _NATIVE_SPECS[engine]

    if engine in _fallback_cache:
        return _fallback_cache[engine]

    # Attempt to find and wrap a sync spec from superset (optional dependency)
    sync_spec_map = _get_sync_spec_map()
    if engine in sync_spec_map:
        async_spec = make_async_spec(sync_spec_map[engine])
        _fallback_cache[engine] = async_spec
        logger.info("Created sync fallback async spec for engine: %s", engine)
        return async_spec

    raise ValueError(f"No async engine spec found for engine: {engine}")


__all__ = [
    "AsyncAthenaEngineSpec",
    "AsyncClickHouseEngineSpec",
    "AsyncCockroachDbEngineSpec",
    "AsyncCrateDbEngineSpec",
    "AsyncDatabendEngineSpec",
    "AsyncDenodoEngineSpec",
    "AsyncDorisEngineSpec",
    "AsyncDynamoDBEngineSpec",
    "AsyncElasticsearchEngineSpec",
    "AsyncFirebirdEngineSpec",
    "AsyncFireboltEngineSpec",
    "AsyncKustoKqlEngineSpec",
    "AsyncKustoSqlEngineSpec",
    "AsyncMSSQLEngineSpec",
    "AsyncMySQLEngineSpec",
    "AsyncOceanBaseEngineSpec",
    "AsyncOpenDistroEngineSpec",
    "AsyncOracleEngineSpec",
    "AsyncPinotEngineSpec",
    "AsyncPostgresEngineSpec",
    "AsyncRedshiftEngineSpec",
    "AsyncResultSet",
    "AsyncRisingWaveEngineSpec",
    "AsyncStarRocksEngineSpec",
    "AsyncTrinoEngineSpec",
    "AsyncYDBEngineSpec",
    "BaseAsyncEngineSpec",
    "SyncFallbackEngineSpec",
    "get_async_engine_spec",
    "make_async_spec",
]
