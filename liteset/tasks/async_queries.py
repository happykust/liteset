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
"""Async query execution Celery tasks for Liteset.

Replaces ``superset/tasks/async_queries.py``. Tasks delegate to the
superset implementation during the Strangler Fig migration.
"""
from __future__ import annotations

import logging
from typing import Any

from liteset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="liteset.tasks.async_queries.load_chart_data_into_cache")
def load_chart_data_into_cache(
    job_metadata: dict[str, Any],
    form_data: dict[str, Any],
) -> None:
    """Load chart data into cache for async retrieval.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.async_queries import (
        load_chart_data_into_cache as _superset_load_chart_data,
    )

    _superset_load_chart_data(job_metadata, form_data)


@celery_app.task(name="liteset.tasks.async_queries.load_explore_json_into_cache")
def load_explore_json_into_cache(
    job_metadata: dict[str, Any],
    form_data: dict[str, Any],
    response_type: str | None = None,
    force: bool = False,
) -> None:
    """Load explore JSON data into cache for async retrieval.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.async_queries import (
        load_explore_json_into_cache as _superset_load_explore_json,
    )

    _superset_load_explore_json(job_metadata, form_data, response_type, force)
