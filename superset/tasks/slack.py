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
"""Slack notification Celery tasks for Superset.

Self-contained implementation that reads Slack configuration from
:class:`~superset.config.SupersetSettings` and uses ``slack_sdk`` directly.
"""

from __future__ import annotations

import logging

from superset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="superset.tasks.slack.cache_channels")
def cache_channels() -> None:
    """Warm up the Slack channels cache.

    1:1 port of ``superset_old/tasks/slack.py:cache_channels``.

    Calls ``_get_slack_channels(force=True)`` which fetches all channels
    from the Slack API (bypassing the existing cache) and writes the result
    to the Superset cache backend under ``"slack_conversations_list"``.
    If the Slack token is not configured the helper exits early.
    """
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    slack_token = getattr(settings, "slack_api_token", "")
    if not slack_token:
        logger.warning("Slack API token not configured; skipping channel cache warm-up")
        return

    slack_cache_timeout = getattr(settings, "slack_cache_timeout", 1800) or 1800
    retry_count = getattr(settings, "slack_api_rate_limit_retry_count", 2) or 2

    logger.info(
        "Starting Slack channels cache warm-up task "
        "(cache_timeout=%ds, retry_count=%d)",
        slack_cache_timeout,
        retry_count,
    )

    try:
        from superset.controllers.report import _get_slack_channels

        channels = _get_slack_channels(force=True)
        logger.info("Cached %d Slack channels", len(channels))
    except Exception as ex:
        logger.exception(
            "Failed to cache Slack channels: %s. "
            "If this is due to rate limiting, consider increasing "
            "SLACK_API_RATE_LIMIT_RETRY_COUNT.",
            str(ex),
        )
        raise
