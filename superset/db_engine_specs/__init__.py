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
  - ``BaseEngineSpec``  -- base class for all engine specs
  - ``get_engine_spec(backend, driver)`` -- factory to look up an engine spec
  - ``load_engine_specs()`` -- dynamically loads all engine spec modules
"""

from __future__ import annotations

import inspect
import logging
import pkgutil
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Optional

from superset.db_engine_specs.base import BaseEngineSpec

logger = logging.getLogger(__name__)


def is_engine_spec(obj: Any) -> bool:
    """
    Return true if a given object is a DB engine spec.
    """
    return (
        inspect.isclass(obj)
        and issubclass(obj, BaseEngineSpec)
        and obj != BaseEngineSpec
    )


def load_engine_specs() -> list[type[BaseEngineSpec]]:
    """
    Load all engine specs, native and 3rd party.
    """
    engine_specs: list[type[BaseEngineSpec]] = []

    # load standard engines
    db_engine_spec_dir = str(Path(__file__).parent)
    for module_info in pkgutil.iter_modules([db_engine_spec_dir], prefix="."):
        module = import_module(module_info.name, package=__name__)
        engine_specs.extend(
            getattr(module, attr)
            for attr in module.__dict__
            if is_engine_spec(getattr(module, attr))
        )
    # load additional engines from external modules
    for ep in entry_points(group="superset.db_engine_specs"):
        try:
            engine_spec = ep.load()
        except Exception:  # pylint: disable=broad-except
            logger.warning("Unable to load Superset DB engine spec: %s", ep.name)
            continue
        engine_specs.append(engine_spec)

    return engine_specs


def get_engine_spec(
    backend: str,
    driver: Optional[str] = None,
) -> type[BaseEngineSpec]:
    """Return the engine spec for *backend* (and optionally *driver*).

    Falls back to ``BaseEngineSpec`` when no specific spec is registered.
    """
    engine_specs = load_engine_specs()

    if driver is not None:
        for engine_spec in engine_specs:
            if engine_spec.supports_backend(backend, driver):
                return engine_spec

    # check ignoring the driver, in order to support new drivers
    for engine_spec in engine_specs:
        if engine_spec.supports_backend(backend):
            return engine_spec

    # default to the generic DB engine spec
    return BaseEngineSpec


__all__ = [
    "BaseEngineSpec",
    "get_engine_spec",
    "is_engine_spec",
    "load_engine_specs",
]
