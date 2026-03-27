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
"""Thumbnails digest computation and Celery task dispatch."""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)


class AsyncThumbnailsDigest:
    """Compute digest and dispatch Celery screenshot task."""

    @staticmethod
    async def compute_digest(
        url: str,
        user_id: int,
        force: bool = False,
    ) -> str:
        """Compute cache key digest for thumbnail."""
        return hashlib.md5(  # noqa: S324
            f"{url}_{user_id}".encode(),
            usedforsecurity=False,
        ).hexdigest()

    @staticmethod
    async def trigger_screenshot(
        url: str,
        digest: str,
        user_id: int,
        force: bool = False,
    ) -> None:
        """Dispatch Celery task for async screenshot capture."""
        try:
            from superset.tasks.thumbnails import (
                cache_chart_thumbnail,
                cache_dashboard_thumbnail,
            )

            # Determine type from URL and dispatch
            if "/chart/" in url or "/slice/" in url:
                cache_chart_thumbnail.delay(url, digest, force=force)
            else:
                cache_dashboard_thumbnail.delay(url, digest, force=force)
        except ImportError:
            logger.warning(
                "Thumbnail tasks not available — Celery not configured"
            )
