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

"""Pluggable logging configurator — port of
``superset_old/utils/logging_configurator.py`` to Liteset.

Liteset's primary logging entry point is
:func:`superset.logging.configure_logging` (structlog-based, called
from :mod:`superset.app`).  The original Flask code paths exposed
:class:`LoggingConfigurator` so operators could ship their own logging
setup via ``LOGGING_CONFIGURATOR`` in ``superset_config.py``;
:class:`DefaultLoggingConfigurator` was the stock implementation.

We keep the same surface so existing config files keep working: an
operator-provided subclass implements :meth:`configure_logging`, and
``SupersetSettings.logging_configurator`` accepts an instance which is
invoked from the boot path (or from third-party CLIs that still call
the legacy hook).

The Flask-specific bits — importing ``flask.config.Config`` for the
type hint and silencing ``flask_appbuilder`` — have been removed.
The implementation now accepts a plain ``Mapping[str, Any]`` (most
commonly :class:`SupersetSettings` itself, which supports
``__getitem__`` via Pydantic's ``model_dump``-backed mapping access,
or a plain dict).
"""

from __future__ import annotations

import abc
import logging
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def _read(config: Any, key: str, default: Any = None) -> Any:
    """Read a config key from either a Pydantic settings instance or a
    plain mapping.

    The original Flask implementation indexed ``app_config["FOO"]``
    directly; in Liteset operators may pass either a
    :class:`SupersetSettings` instance (snake_case attributes) or a
    plain dict (UPPER_CASE keys).  We try both surfaces transparently
    so a downstream subclass that still does ``cfg["LOG_FORMAT"]``
    keeps working.
    """
    # Mapping-style access (dict, or any subclass) — try original
    # UPPER_CASE first, then snake_case.
    if isinstance(config, Mapping):
        if key in config:
            return config[key]
        snake = key.lower()
        if snake in config:
            return config[snake]
        return default
    # Settings-style access (Pydantic).
    snake = key.lower()
    if hasattr(config, snake):
        return getattr(config, snake)
    if hasattr(config, key):
        return getattr(config, key)
    return default


class LoggingConfigurator(abc.ABC):  # pylint: disable=too-few-public-methods
    """Abstract logging configurator.

    Operators wanting a custom logging setup point
    ``LOGGING_CONFIGURATOR`` (or ``logging_configurator`` in
    snake_case) at a subclass instance.
    """

    @abc.abstractmethod
    def configure_logging(self, app_config: Any, debug_mode: bool) -> None:
        """Configure root / package loggers.

        :param app_config: Either a :class:`SupersetSettings` instance
            or a plain ``Mapping[str, Any]`` of config values.
        :param debug_mode: Whether the app is running in debug mode.
        """


class DefaultLoggingConfigurator(  # pylint: disable=too-few-public-methods
    LoggingConfigurator
):
    """Stock logging configurator — installs a stderr ``StreamHandler``
    plus an optional :class:`TimedRotatingFileHandler`.

    Mirrors the original implementation byte-for-byte except for the
    Flask-AppBuilder logger silencing (no longer applicable).
    """

    def configure_logging(self, app_config: Any, debug_mode: bool) -> None:
        # ``basicConfig()`` will set up a default StreamHandler on stderr.
        logging.basicConfig(format=_read(app_config, "LOG_FORMAT"))
        logging.getLogger().setLevel(_read(app_config, "LOG_LEVEL"))

        if _read(app_config, "ENABLE_TIME_ROTATE", False):
            logging.getLogger().setLevel(_read(app_config, "TIME_ROTATE_LOG_LEVEL"))
            handler = TimedRotatingFileHandler(
                _read(app_config, "FILENAME"),
                when=_read(app_config, "ROLLOVER"),
                interval=_read(app_config, "INTERVAL"),
                backupCount=_read(app_config, "BACKUP_COUNT"),
            )
            logging.getLogger().addHandler(handler)

        logger.debug("logging was configured successfully")
