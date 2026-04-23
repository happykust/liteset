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
"""Async port of ``superset_old/commands/chart/data/create_async_job_command.py``.

1:1 with the original two-method signature:

* ``validate(request)`` — parses the channel id from the request's
  ``GLOBAL_ASYNC_QUERIES_JWT_COOKIE_NAME`` cookie, mirroring
  ``async_query_manager.parse_channel_id_from_request``.
* ``run(form_data, user_id)`` — enqueues the
  ``load_chart_data_into_cache`` Celery task and returns the
  ``job_metadata`` dict (``channel_id`` / ``job_id`` / ``user_id`` /
  ``status="pending"`` / ``errors`` / ``result_url``), mirroring
  ``async_query_manager.submit_chart_data_job``.
"""

from __future__ import annotations

import logging
from typing import Any

import jwt

logger = logging.getLogger(__name__)


class AsyncQueryTokenException(Exception):  # noqa: N818  # 1:1 with original public API
    """Raised when the JWT channel-token cookie is missing or invalid.

    1:1 with
    ``superset_old.async_events.async_query_manager.AsyncQueryTokenException``.
    """


class CreateAsyncChartDataJobCommand:
    """Submit an async chart-data job by writing to Celery.

    1:1 with
    ``superset_old.commands.chart.data.create_async_job_command.CreateAsyncChartDataJobCommand``.

    The original used the legacy global ``async_query_manager`` singleton.
    The async port relies on the request's JWT-encoded channel cookie
    (parsed inside ``validate``) and the ``load_chart_data_into_cache``
    Celery task wrapped by :class:`AsyncEventManager` (called from ``run``).
    """

    _async_channel_id: str

    def __init__(
        self,
        jwt_secret: str | None = None,
        jwt_cookie_name: str = "async-token",
    ) -> None:
        # Defaults match the original config keys ``GLOBAL_ASYNC_QUERIES_JWT_SECRET``
        # / ``GLOBAL_ASYNC_QUERIES_JWT_COOKIE_NAME`` so the controller can
        # construct the command with the same arguments the original
        # ``async_query_manager`` was bootstrapped with.
        self._jwt_secret = jwt_secret
        self._jwt_cookie_name = jwt_cookie_name

    def validate(self, request: Any) -> None:
        """Parse the channel id from the request's JWT cookie.

        1:1 with
        ``async_query_manager.parse_channel_id_from_request`` —
        reads ``request.cookies[self._jwt_cookie_name]``, decodes the
        HS256 JWT with ``self._jwt_secret`` and stores
        ``payload["channel"]`` as ``self._async_channel_id``.
        """
        cookies = getattr(request, "cookies", None) or {}
        token = cookies.get(self._jwt_cookie_name) if hasattr(cookies, "get") else None
        if not token:
            raise AsyncQueryTokenException("Token not preset")
        try:
            payload = jwt.decode(token, self._jwt_secret, algorithms=["HS256"])
        except Exception as ex:  # noqa: BLE001
            logger.warning("Parse jwt failed", exc_info=True)
            raise AsyncQueryTokenException("Failed to parse token") from ex
        self._async_channel_id = payload["channel"]

    def run(
        self,
        form_data: dict[str, Any],
        user_id: int | None,
    ) -> dict[str, Any]:
        """Enqueue the Celery chart-data job and return its metadata.

        1:1 with ``async_query_manager.submit_chart_data_job`` —
        builds the ``job_metadata`` envelope and dispatches
        ``load_chart_data_into_cache.delay(job_metadata, form_data)``.
        """
        # Imported lazily to avoid pulling celery at module import time
        # (the command is constructed inside HTTP request handlers that
        # may run in environments where celery isn't even installed).
        import uuid

        from superset.async_events.manager import build_job_metadata
        from superset.tasks.async_queries import load_chart_data_into_cache

        job_id = str(uuid.uuid4())
        job_metadata = build_job_metadata(
            channel_id=self._async_channel_id,
            job_id=job_id,
            user_id=user_id,
            status="pending",
        )
        load_chart_data_into_cache.delay(job_metadata, form_data)
        return job_metadata
