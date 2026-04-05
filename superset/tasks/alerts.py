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
"""Alert evaluation Celery tasks for Superset.

Provides an ``alerts`` namespace entry point that delegates to
:func:`superset.tasks.scheduler.execute` for backward compatibility with
beat schedules that reference the old ``superset.tasks.alerts.execute``
task name.
"""

from __future__ import annotations

import logging

from celery import Task

from superset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="superset.tasks.alerts.execute", bind=True)
def execute(self: Task, report_schedule_id: int) -> None:
    """Execute a single alert/report schedule.

    This is an alias entry point for ``superset.tasks.scheduler.execute``
    exposed under the ``alerts`` namespace for backward compatibility
    with beat schedules that reference ``superset.tasks.alerts.execute``.
    """
    from superset.tasks.scheduler import execute as scheduler_execute

    scheduler_execute(report_schedule_id)
