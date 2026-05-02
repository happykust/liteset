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
"""Module-level extension singletons (mirrors ``superset_old/extensions``).

Hosts the singletons that legacy code expects to import from
``superset.extensions``:

* ``encrypted_field_factory`` — async-friendly port of
  :class:`superset.utils.encrypt.EncryptedFieldFactory` used by SQLA
  models to declare encrypted columns.
* ``machine_auth_provider_factory`` — full port of the Flask
  ``MachineAuthProviderFactory``; initialised in
  :func:`superset.app.on_startup` and consumed by ``utils/webdriver``
  and the Celery report task to mint screenshot/CSV-fetch cookies.
* ``cache_manager`` — multi-cache holder (``cache``, ``data_cache``,
  ``thumbnail_cache`` …); see :class:`superset.cache.manager.CacheManager`.
* ``stats_logger_manager`` — process-wide stats-logger holder; default
  is a :class:`~superset.stats_logger.DummyStatsLogger`, swapped to
  whatever ``settings.stats_logger`` provides during app startup.
"""

from __future__ import annotations

from superset.cache.manager import CacheManager
from superset.stats_logger import StatsLoggerManager
from superset.utils.encrypt import EncryptedFieldFactory
from superset.utils.machine_auth import MachineAuthProviderFactory

# ---------------------------------------------------------------------------
# Singletons.  All are initialised lazily; ``superset.app.on_startup``
# wires real Redis / settings into them at runtime.  Tests / CLI tools
# that don't go through ``on_startup`` get the default (no-op) behaviour.
# ---------------------------------------------------------------------------

encrypted_field_factory = EncryptedFieldFactory()

# Configured by ``superset.app.on_startup`` once settings are loaded.
# Webdriver / Celery report code reads this lazily via ``.instance``.
machine_auth_provider_factory = MachineAuthProviderFactory()

cache_manager = CacheManager()

stats_logger_manager = StatsLoggerManager()


__all__ = [
    "cache_manager",
    "encrypted_field_factory",
    "machine_auth_provider_factory",
    "stats_logger_manager",
]
