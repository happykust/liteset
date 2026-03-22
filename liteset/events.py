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
"""Audit event logging for liteset API endpoints."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class EventLogger:
    """Minimal audit event logger.

    Logs API actions as structured log records.  Drop-in replacement
    candidates (DataDog, OpenTelemetry, database table) can override
    ``log()``.
    """

    def log(
        self,
        action: str,
        *,
        object_ref: str | None = None,
        user_id: int | None = None,
        duration_ms: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"action": action}
        if object_ref is not None:
            payload["object_ref"] = object_ref
        if user_id is not None:
            payload["user_id"] = user_id
        if duration_ms is not None:
            payload["duration_ms"] = round(duration_ms, 2)
        if extra:
            payload.update(extra)
        logger.info("event_log %s", payload)


# Module-level singleton
event_logger = EventLogger()
