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
"""Stats logger primitives.

Provides ``BaseStatsLogger`` (interface), ``DummyStatsLogger`` (no-op
default) and a best-effort ``StatsdStatsLogger`` that degrades gracefully
when the ``statsd`` package is missing.

Used by:
- :mod:`superset.utils.decorators` (``@statsd_gauge``)
- :mod:`superset.utils.cache` (``set_and_log_cache``)
- :mod:`superset.utils.log` / :mod:`superset.events`
  (``log_to_statsd`` event-logger flag)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# colorama is an optional dependency; the original Superset always pulled
# it in via the legacy WSGI stack but the Liteset stack does not.  Fall back
# to blank ANSI escapes when it isn't installed so log output is unstyled
# but still legible.
try:  # pragma: no cover — depends on env
    from colorama import Fore, Style

    _HAS_COLORAMA = True
except ImportError:  # pragma: no cover
    _HAS_COLORAMA = False

    class _NullColor:
        CYAN = ""
        RESET_ALL = ""

    Fore = _NullColor()
    Style = _NullColor()


class BaseStatsLogger:
    """Base class for realtime stats logging."""

    def __init__(self, prefix: str = "superset") -> None:
        self.prefix = prefix

    def key(self, key: str) -> str:
        if self.prefix:
            return self.prefix + key
        return key

    def incr(self, key: str) -> None:
        raise NotImplementedError()

    def decr(self, key: str) -> None:
        raise NotImplementedError()

    def timing(self, key: str, value: float) -> None:
        raise NotImplementedError()

    def gauge(self, key: str, value: float) -> None:
        raise NotImplementedError()


class DummyStatsLogger(BaseStatsLogger):
    """No-op default — emits cyan-coloured debug log lines.

    Every call concatenates the cyan ANSI escape (``Fore.CYAN``), the
    message, and ``Style.RESET_ALL`` so the lines stand out in stdout.
    When ``colorama`` is missing the escapes degrade to empty strings (see
    module-level fallback).
    """

    def incr(self, key: str) -> None:
        logger.debug(Fore.CYAN + "[stats_logger] (incr) " + key + Style.RESET_ALL)

    def decr(self, key: str) -> None:
        logger.debug(Fore.CYAN + "[stats_logger] (decr) " + key + Style.RESET_ALL)

    def timing(self, key: str, value: float) -> None:
        logger.debug(
            Fore.CYAN + f"[stats_logger] (timing) {key} | {value} " + Style.RESET_ALL
        )

    def gauge(self, key: str, value: float) -> None:
        logger.debug(
            Fore.CYAN
            + "[stats_logger] (gauge) "
            + f"{key}"
            + f"{value}"
            + Style.RESET_ALL
        )


try:  # pragma: no cover - optional dependency
    from statsd import StatsClient

    class StatsdStatsLogger(BaseStatsLogger):
        def __init__(  # pylint: disable=super-init-not-called
            self,
            host: str = "localhost",
            port: int = 8125,
            prefix: str = "superset",
            statsd_client: Any = None,
        ) -> None:
            if statsd_client:
                self.client = statsd_client
            else:
                self.client = StatsClient(host=host, port=port, prefix=prefix)

        def incr(self, key: str) -> None:
            self.client.incr(key)

        def decr(self, key: str) -> None:
            self.client.decr(key)

        def timing(self, key: str, value: float) -> None:
            self.client.timing(key, value)

        def gauge(self, key: str, value: float) -> None:
            self.client.gauge(key, value)

except Exception as _ex:  # pragma: no cover - reraised on instantiation only
    # Defer the import error to instantiation time; keep the original traceback.
    _saved_exception = _ex

    class StatsdStatsLogger(BaseStatsLogger):  # type: ignore[no-redef]
        """Stub raised on instantiation when ``statsd`` is unavailable."""

        def __init__(  # pylint: disable=super-init-not-called
            self,
            host: str = "localhost",
            port: int = 8125,
            prefix: str = "superset",
            statsd_client: Any = None,
        ) -> None:
            raise _saved_exception


class StatsLoggerManager:
    """Process-wide stats-logger holder.

    Replaces ``BaseStatsLoggerManager.init_app(app)``.  Liteset bootstrap
    code calls :meth:`configure` once during :func:`superset.app.on_startup`
    with the value loaded from :class:`superset.config.SupersetSettings`.
    """

    def __init__(self) -> None:
        self._stats_logger: BaseStatsLogger = DummyStatsLogger()

    def configure(self, stats_logger: BaseStatsLogger | None) -> None:
        """Replace the active logger; ``None`` resets to ``DummyStatsLogger``."""
        if stats_logger is None:
            self._stats_logger = DummyStatsLogger()
        else:
            self._stats_logger = stats_logger

    @property
    def instance(self) -> BaseStatsLogger:
        return self._stats_logger

    def incr(self, key: str) -> None:
        self._stats_logger.incr(key)

    def decr(self, key: str) -> None:
        self._stats_logger.decr(key)

    def timing(self, key: str, value: float) -> None:
        self._stats_logger.timing(key, value)

    def gauge(self, key: str, value: float) -> None:
        self._stats_logger.gauge(key, value)


__all__ = [
    "BaseStatsLogger",
    "DummyStatsLogger",
    "StatsdStatsLogger",
    "StatsLoggerManager",
]
