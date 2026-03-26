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
"""Cache warming Celery tasks for Liteset.

Replaces ``superset/tasks/cache.py``. Tasks are registered with
``liteset.tasks.*`` names; the old ``superset.tasks.*`` names are
aliased via :func:`~liteset.tasks.celery_app.register_task_aliases`.

Strategy classes (``DummyStrategy``, ``TopNDashboardsStrategy``,
``DashboardTagsStrategy``) are simplified stubs -- full implementations
delegate to the superset originals during the Strangler Fig migration.
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from celery.utils.log import get_task_logger

from liteset.tasks.celery_app import celery_app

logger = get_task_logger(__name__)
logger.setLevel(logging.INFO)


class CacheWarmupPayload(TypedDict, total=False):
    chart_id: int
    dashboard_id: int | None


class CacheWarmupTask(TypedDict):
    payload: CacheWarmupPayload
    username: str | None


@celery_app.task(name="liteset.tasks.cache.fetch_url")
def fetch_url(data: str, headers: dict[str, str]) -> dict[str, str]:
    """Fetch a URL to warm up the chart cache.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.cache import fetch_url as _superset_fetch_url

    return _superset_fetch_url(data, headers)


@celery_app.task(name="liteset.tasks.cache.cache_warmup")
def cache_warmup(
    strategy_name: str, *args: Any, **kwargs: Any
) -> dict[str, list[str]] | str:
    """Warm up cache using the specified strategy.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.cache import cache_warmup as _superset_cache_warmup

    return _superset_cache_warmup(strategy_name, *args, **kwargs)
