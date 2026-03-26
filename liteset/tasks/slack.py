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
"""Slack notification Celery tasks for Liteset.

Replaces ``superset/tasks/slack.py``. Provides the Slack channel cache
warm-up task registered under the ``liteset.tasks.*`` namespace.
"""
from __future__ import annotations

import logging

from liteset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="liteset.tasks.slack.cache_channels")
def cache_channels() -> None:
    """Warm up the Slack channels cache.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.slack import cache_channels as _superset_cache_channels

    _superset_cache_channels()
