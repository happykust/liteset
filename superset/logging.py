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
"""Structured logging configuration via structlog."""

from __future__ import annotations

import logging
import logging.config

import structlog

from superset.config import SupersetSettings

# Paths whose access-log entries are high-volume and low-signal — mostly
# the dev-mode HMR WebSocket and the long-lived /ws event channel. Filtered
# from uvicorn.access so the console stays readable.
_NOISY_ACCESS_PATHS: tuple[str, ...] = (
    '"WebSocket /ws-hmr"',
    '"WebSocket /ws"',
    '"WebSocket /ws?',
)


class _UvicornAccessFilter(logging.Filter):
    """Drop access-log records for noisy WebSocket paths."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access formats its message as
        # '%s - "%s %s HTTP/%s" %d' with args=(client, method, path, ...)
        # For WebSocket connections the message is
        # '%s - "WebSocket %s" [accepted|403]'.
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        return not any(noisy in msg for noisy in _NOISY_ACCESS_PATHS)


class _WebsocketLifecycleFilter(logging.Filter):
    """Drop the 'connection open' / 'connection closed' / 'connection
    rejected' records that uvicorn.error emits for every WebSocket connect
    and disconnect. These are not errors and not actionable.
    """

    _MESSAGES: frozenset[str] = frozenset(
        {
            "connection open",
            "connection closed",
            "connection rejected (403 Forbidden)",
        }
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        return msg.strip() not in self._MESSAGES


def configure_logging(settings: SupersetSettings) -> None:
    """Configure structured logging.

    JSON in production, colorized console in development.
    """
    log_level = settings.log_level.upper()

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "uvicorn_access_filter": {
                    "()": _UvicornAccessFilter,
                },
                "websocket_lifecycle_filter": {
                    "()": _WebsocketLifecycleFilter,
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "level": log_level,
                },
            },
            "root": {"handlers": ["default"], "level": log_level},
        }
    )

    # Attach filters programmatically so they apply regardless of which
    # handlers uvicorn (or Litestar, or any other library) installs on
    # these loggers at startup. Removing existing handlers first ensures
    # that every emission path goes through *our* stream handler, which
    # inherits the root formatter. Belt-and-braces: also attach filters at
    # the handler level so even records bypassing logger filters still get
    # caught.
    access_filter = _UvicornAccessFilter()
    lifecycle_filter = _WebsocketLifecycleFilter()

    for name, filt in (
        ("uvicorn.access", access_filter),
        ("uvicorn.error", lifecycle_filter),
        ("websockets", lifecycle_filter),
        ("websockets.server", lifecycle_filter),
    ):
        lg = logging.getLogger(name)
        # Clear any pre-existing handlers so log records don't escape via
        # a duplicate uvicorn handler without the filter.
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.addFilter(filt)
        lg.propagate = True  # let root handler emit after filter passes

    # Also attach both filters to the root stream handler so that records
    # reaching root (via propagation) are filtered there too.
    root_logger = logging.getLogger()
    for h in root_logger.handlers:
        h.addFilter(access_filter)
        h.addFilter(lifecycle_filter)

    shared_processors: list[structlog.types.Processor] = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    processors: list[structlog.types.Processor]
    if settings.production:
        processors = [
            *shared_processors,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            *shared_processors,
            structlog.processors.ExceptionRenderer(),
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
