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
"""Tests for AsyncThumbnailsDigest."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

from liteset.thumbnails.digest import AsyncThumbnailsDigest


async def test_compute_digest_returns_md5() -> None:
    """compute_digest returns an MD5 hex digest of url_userId."""
    digest = await AsyncThumbnailsDigest.compute_digest(
        url="http://localhost/chart/1", user_id=42
    )
    expected = hashlib.md5("http://localhost/chart/1_42".encode()).hexdigest()
    assert digest == expected


async def test_compute_digest_different_urls() -> None:
    """Different URLs produce different digests."""
    d1 = await AsyncThumbnailsDigest.compute_digest(url="/chart/1", user_id=1)
    d2 = await AsyncThumbnailsDigest.compute_digest(url="/chart/2", user_id=1)
    assert d1 != d2


async def test_compute_digest_different_users() -> None:
    """Different user IDs produce different digests."""
    d1 = await AsyncThumbnailsDigest.compute_digest(url="/chart/1", user_id=1)
    d2 = await AsyncThumbnailsDigest.compute_digest(url="/chart/1", user_id=2)
    assert d1 != d2


async def test_trigger_screenshot_chart() -> None:
    """trigger_screenshot dispatches chart thumbnail task for chart URLs."""
    mock_task = MagicMock()
    with patch.dict(
        "sys.modules",
        {
            "superset": MagicMock(),
            "superset.tasks": MagicMock(),
            "superset.tasks.thumbnails": MagicMock(
                cache_chart_thumbnail=mock_task,
                cache_dashboard_thumbnail=MagicMock(),
            ),
        },
    ):
        await AsyncThumbnailsDigest.trigger_screenshot(
            url="http://localhost/chart/1",
            digest="abc123",
            user_id=1,
        )
        mock_task.delay.assert_called_once_with(
            "http://localhost/chart/1", "abc123", force=False
        )


async def test_trigger_screenshot_dashboard() -> None:
    """trigger_screenshot dispatches dashboard thumbnail task for non-chart URLs."""
    mock_task = MagicMock()
    with patch.dict(
        "sys.modules",
        {
            "superset": MagicMock(),
            "superset.tasks": MagicMock(),
            "superset.tasks.thumbnails": MagicMock(
                cache_chart_thumbnail=MagicMock(),
                cache_dashboard_thumbnail=mock_task,
            ),
        },
    ):
        await AsyncThumbnailsDigest.trigger_screenshot(
            url="http://localhost/dashboard/5",
            digest="def456",
            user_id=2,
        )
        mock_task.delay.assert_called_once_with(
            "http://localhost/dashboard/5", "def456", force=False
        )


async def test_trigger_screenshot_slice_url() -> None:
    """trigger_screenshot treats /slice/ URLs as chart thumbnails."""
    mock_task = MagicMock()
    with patch.dict(
        "sys.modules",
        {
            "superset": MagicMock(),
            "superset.tasks": MagicMock(),
            "superset.tasks.thumbnails": MagicMock(
                cache_chart_thumbnail=mock_task,
                cache_dashboard_thumbnail=MagicMock(),
            ),
        },
    ):
        await AsyncThumbnailsDigest.trigger_screenshot(
            url="http://localhost/slice/3",
            digest="ghi789",
            user_id=1,
            force=True,
        )
        mock_task.delay.assert_called_once_with(
            "http://localhost/slice/3", "ghi789", force=True
        )


async def test_trigger_screenshot_handles_import_error() -> None:
    """trigger_screenshot logs warning when Celery tasks are unavailable."""
    # This should not raise — ImportError is caught internally
    await AsyncThumbnailsDigest.trigger_screenshot(
        url="http://localhost/chart/1",
        digest="abc",
        user_id=1,
    )
