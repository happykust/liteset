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
"""Engine-spec registry for sync-compatible engine specs.

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


# There's a mismatch between the dialect name reported by the driver in these
# libraries and the dialect name used in the URI — 1:1 with upstream
# ``backend_replacements``.
_BACKEND_REPLACEMENTS = {
    "drilldbapi": "drill",
    "exasol": "exa",
}


def get_installed_drivers() -> dict[str, set[str]]:  # noqa: C901
    """Map engine/backend name → set of INSTALLED driver names.

    1:1 port of the driver-discovery half of upstream
    ``get_available_engine_specs`` (superset_old/db_engine_specs/__init__.py:
    125-168): native SQLAlchemy dialects whose ``dbapi()`` import succeeds,
    plus 3rd-party dialects registered via the ``sqlalchemy.dialects``
    entry-point group.  Consumed by ``GET /api/v1/database/available/`` so
    the "Connect a database" modal only offers engines that can actually
    connect (R11-17).
    """
    from collections import defaultdict
    from importlib.metadata import entry_points as _entry_points

    import sqlalchemy.dialects
    from sqlalchemy.engine.default import DefaultDialect
    from sqlalchemy.exc import NoSuchModuleError

    drivers: dict[str, set[str]] = defaultdict(set)

    # native SQLAlchemy dialects
    for attr in sqlalchemy.dialects.__all__:
        try:
            dialect = sqlalchemy.dialects.registry.load(attr)
            if (
                issubclass(dialect, DefaultDialect)
                and hasattr(dialect, "driver")
                # adodbapi dialect is removed in SQLA 1.4 and doesn't
                # implement ``dbapi``; ignore to avoid a warning.
                and dialect.driver != "adodbapi"
            ):
                _load_dbapi = getattr(dialect, "import_dbapi", None) or getattr(
                    dialect, "dbapi", None
                )
                if _load_dbapi is None:
                    continue
                try:
                    _load_dbapi()
                except ModuleNotFoundError:
                    continue
                except Exception as ex:  # noqa: BLE001
                    logger.warning("Unable to load dialect %s: %s", dialect, ex)
                    continue
                drivers[attr].add(dialect.driver)
        except (NoSuchModuleError, ModuleNotFoundError):
            continue

    # installed 3rd-party dialects
    for ep in _entry_points(group="sqlalchemy.dialects"):
        try:
            dialect = ep.load()
        except Exception as ex:  # noqa: BLE001
            logger.debug("Unable to load SQLAlchemy dialect %s: %s", ep.name, ex)
        else:
            backend = dialect.name
            if isinstance(backend, bytes):
                backend = backend.decode()
            backend = _BACKEND_REPLACEMENTS.get(backend, backend)

            driver = getattr(dialect, "driver", dialect.name)
            if isinstance(driver, bytes):
                driver = driver.decode()
            drivers[backend].add(driver)

    return dict(drivers)
