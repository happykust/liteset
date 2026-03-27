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
"""Cache warming Celery tasks for Superset.

Self-contained implementations with no superset dependency.
:func:`fetch_url` performs an HTTP PUT to warm chart caches.
:func:`cache_warmup` is a strategy-based stub for future expansion.
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict
from urllib import request
from urllib.error import URLError

from celery.utils.log import get_task_logger

from superset.tasks.celery_app import celery_app

logger = get_task_logger(__name__)
logger.setLevel(logging.INFO)


class CacheWarmupPayload(TypedDict, total=False):
    chart_id: int
    dashboard_id: int | None


class CacheWarmupTask(TypedDict):
    payload: CacheWarmupPayload
    username: str | None


@celery_app.task(name="superset.tasks.cache.fetch_url")
def fetch_url(data: str, headers: dict[str, str]) -> dict[str, str]:
    """Fetch a URL to warm up the chart cache.

    Sends an HTTP PUT request with the provided *data* payload and
    *headers*.  Returns a dict indicating success or failure.
    """
    result: dict[str, str] = {}
    try:
        logger.info("Fetching cache warmup with payload %s", data)
        req = request.Request(  # noqa: S310
            data,
            data=bytes(data, "utf-8"),
            headers=headers,
            method="PUT",
        )
        response = request.urlopen(req, timeout=600)  # noqa: S310
        logger.info("Fetched with payload %s, status code: %s", data, response.code)
        if response.code == 200:
            result = {"success": data, "response": response.read().decode("utf-8")}
        else:
            result = {"error": data, "status_code": str(response.code)}
            logger.error(
                "Error fetching with payload %s, status code: %s",
                data,
                response.code,
            )
    except URLError:
        logger.exception("Error fetching cache warmup URL with payload %s", data)
        result = {"error": data}
    return result


@celery_app.task(name="superset.tasks.cache.cache_warmup")
def cache_warmup(
    strategy_name: str, *args: Any, **kwargs: Any
) -> list[dict[str, Any]]:
    """Warm up cache using the specified strategy.

    Strategy classes (TopNDashboards, DashboardTags, etc.) are not yet
    ported.  This stub logs the invocation and returns an empty list.
    """
    logger.info("Cache warmup requested with strategy: %s", strategy_name)
    # TODO: implement strategy classes (DummyStrategy, TopNDashboardsStrategy,
    # DashboardTagsStrategy) using superset DAOs once available.
    return []
