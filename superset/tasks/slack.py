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

    Reads Slack API token from superset configuration and fetches the
    channel list via ``slack_sdk``.  If the Slack token is not configured,
    the task exits early with a warning.
    """
    from superset.config import SupersetSettings

    settings = SupersetSettings()
    slack_token = getattr(settings, "slack_api_token", "")

    if not slack_token:
        logger.warning(
            "Slack API token not configured; skipping channel cache warm-up"
        )
        return

    logger.info("Starting Slack channels cache warm-up task")
    try:
        from slack_sdk import WebClient

        client = WebClient(token=slack_token)
        response = client.conversations_list(
            types="public_channel,private_channel",
            limit=1000,
            exclude_archived=True,
        )
        channels = response.get("channels", [])
        logger.info("Cached %d Slack channels", len(channels))
        # TODO: store channels in superset cache backend for quick lookup
        # by notification tasks.
    except ImportError:
        logger.warning(
            "slack_sdk is not installed; cannot cache Slack channels. "
            "Install with: pip install slack_sdk"
        )
    except Exception:
        logger.exception(
            "Failed to cache Slack channels. "
            "If this is due to rate limiting, consider retrying later."
        )
        raise
