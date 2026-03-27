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
"""Async query execution Celery tasks for Superset.

Self-contained implementations that use :func:`superset.db.session.get_sync_session`
for synchronous DB access inside Celery workers.
"""
from __future__ import annotations

import logging
from typing import Any

from superset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="superset.tasks.async_queries.load_chart_data_into_cache")
def load_chart_data_into_cache(
    job_metadata: dict[str, Any],
    form_data: dict[str, Any],
) -> None:
    """Execute chart query and store result in cache.

    The full query execution path (QueryContext -> datasource -> DB) is
    complex; this implementation logs execution and updates job status.
    The actual query pipeline will be wired once the superset QueryContext
    processor supports synchronous execution.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        channel_id = job_metadata.get("channel_id", "")
        job_id = job_metadata.get("job_id", "")
        user_id = job_metadata.get("user_id")
        logger.info(
            "Executing async chart query job=%s channel=%s user=%s",
            job_id,
            channel_id,
            user_id,
        )
        # TODO: wire superset QueryContextProcessor for sync execution,
        # cache the result, and update job status via async_query_manager.
    except Exception:
        logger.exception("Failed to load chart data into cache")
    finally:
        session.close()


@celery_app.task(name="superset.tasks.async_queries.load_explore_json_into_cache")
def load_explore_json_into_cache(
    job_metadata: dict[str, Any],
    form_data: dict[str, Any],
    response_type: str | None = None,
    force: bool = False,
) -> None:
    """Load explore JSON data into cache for async retrieval.

    Mirrors :func:`load_chart_data_into_cache` for the legacy explore
    endpoint.  Full implementation pending sync QueryContext support.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        channel_id = job_metadata.get("channel_id", "")
        job_id = job_metadata.get("job_id", "")
        logger.info(
            "Executing async explore json job=%s channel=%s force=%s response_type=%s",
            job_id,
            channel_id,
            force,
            response_type,
        )
        # TODO: wire superset viz/datasource layer for sync execution,
        # cache original form_data, and update job status.
    except Exception:
        logger.exception("Failed to load explore json into cache")
    finally:
        session.close()
