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
"""Engine-spec registry for sync/Flask-compatible engine specs.

Provides:
  - ``BaseEngineSpec``  — base class for all engine specs
  - ``get_engine_spec(backend, driver)`` — factory to look up an engine spec
  - ``PostgresEngineSpec`` — PostgreSQL-specific overrides
"""

from __future__ import annotations

from superset.db_engine_specs.base import BaseEngineSpec
from superset.db_engine_specs.postgres import (
    PostgresBaseEngineSpec,
    PostgresEngineSpec,
)


# ---------------------------------------------------------------------------
# Registry of known engine specs keyed by engine name + aliases.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[BaseEngineSpec]] = {}


def _register(spec: type[BaseEngineSpec]) -> None:
    """Register a spec under its ``engine`` and all ``engine_aliases``."""
    if engine := getattr(spec, "engine", ""):
        _REGISTRY[engine] = spec
    for alias in getattr(spec, "engine_aliases", set()):
        _REGISTRY[alias] = spec


# Register known specs.
_register(PostgresEngineSpec)


def get_engine_spec(
    backend: str,
    driver: str | None = None,
) -> type[BaseEngineSpec]:
    """Return the engine spec for *backend* (and optionally *driver*).

    Falls back to ``BaseEngineSpec`` when no specific spec is registered.
    """
    if spec := _REGISTRY.get(backend):
        return spec
    return BaseEngineSpec


__all__ = [
    "BaseEngineSpec",
    "PostgresBaseEngineSpec",
    "PostgresEngineSpec",
    "get_engine_spec",
]
