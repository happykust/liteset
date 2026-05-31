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
"""Content-type regression for the thumbnail / screenshot endpoints.

The frontend ``ImageLoader``
(``packages/superset-ui-core/src/components/ListViewCard/ImageLoader.tsx``)
fetches the thumbnail URL, reads the response as a blob, and shows the image
only when ``/image/.test(blob.type)`` — i.e. it discriminates purely on the
``Content-Type``.  The original Flask API returns ``image/png`` ONLY for a
real image and ``application/json`` (``response_404`` / ``response(202, ...)``)
on every miss / pending task.

The port had returned an *empty* ``image/png`` body on the 404 / 202 paths,
which would pass the ``ImageLoader`` regex and render a blank/broken tile
instead of the fallback placeholder.  These tests pin the faithful behaviour:
non-image responses carry ``application/json``, never ``image/png``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.controllers.chart import ChartController
from superset.controllers.dashboard import DashboardController
from superset.exceptions import ObjectNotFoundError


def _raw(controller_cls: type, name: str) -> Any:
    handler = getattr(controller_cls, name)
    return handler.fn if hasattr(handler, "fn") else handler


_chart_thumbnail = _raw(ChartController, "thumbnail")
_chart_screenshot = _raw(ChartController, "screenshot")
_dashboard_thumbnail = _raw(DashboardController, "thumbnail")
_dashboard_screenshot = _raw(DashboardController, "screenshot")


def _state(flags: dict[str, bool]) -> MagicMock:
    state = MagicMock()
    state.settings.feature_flags = flags
    return state


# ---------------------------------------------------------------------------
# Feature-flag-off paths return JSON 404 (not an empty image/png)
# ---------------------------------------------------------------------------


async def test_chart_thumbnail_flag_off_returns_json_404() -> None:
    resp = await _chart_thumbnail(
        ChartController(owner=MagicMock()),
        pk=1,
        digest="d",
        dao=AsyncMock(),
        state=_state({}),  # THUMBNAILS off
        current_user=MagicMock(),
        security_manager=AsyncMock(),
    )
    assert resp.status_code == 404
    assert resp.media_type == "application/json"


async def test_chart_screenshot_flag_off_returns_json_404() -> None:
    resp = await _chart_screenshot(
        ChartController(owner=MagicMock()),
        pk=1,
        digest="d",
        dao=AsyncMock(),
        state=_state({}),
        security_manager=AsyncMock(),
        current_user=MagicMock(),
    )
    assert resp.status_code == 404
    assert resp.media_type == "application/json"


async def test_dashboard_thumbnail_flag_off_raises_not_found() -> None:
    # The dashboard thumbnail flag-off path raises ObjectNotFoundError, which
    # the generic handler renders as JSON — never an empty image/png.
    with pytest.raises(ObjectNotFoundError):
        await _dashboard_thumbnail(
            DashboardController(owner=MagicMock()),
            pk=1,
            digest="d",
            dao=AsyncMock(),
            state=_state({}),
            current_user=MagicMock(),
            security_manager=AsyncMock(),
        )


# ---------------------------------------------------------------------------
# 202 (task queued) path returns JSON, not an empty image/png
# ---------------------------------------------------------------------------


@patch("superset.tasks.thumbnails.cache_chart_thumbnail")
@patch("superset.utils.screenshots.ChartScreenshot")
async def test_chart_thumbnail_pending_returns_json_202(
    mock_cs: MagicMock,
    mock_task: MagicMock,
) -> None:
    """A queued thumbnail returns 202 + application/json (upstream task_* keys)."""
    mock_task.delay = MagicMock()
    inst = mock_cs.return_value
    inst.get_cache_key.return_value = "ck123"
    inst.cache.set = MagicMock()
    # Cache miss → fresh ScreenshotCachePayload() → should_trigger_task() True.
    mock_cs.get_from_cache_key.return_value = None

    chart = MagicMock()
    chart.digest = "d"  # equals requested digest → no redirect
    chart.id = 1
    dao = AsyncMock()
    # The handler now uses an access-scoped find_all (not find_by_id_with_options).
    dao.find_all = AsyncMock(return_value=[chart])

    with patch(
        "superset.db.filters.chart_access_filters",
        new=AsyncMock(return_value=[]),
    ):
        resp = await _chart_thumbnail(
            ChartController(owner=MagicMock()),
            pk=1,
            digest="d",
            dao=dao,
            state=_state({"THUMBNAILS": True}),
            current_user=MagicMock(),
            security_manager=AsyncMock(),
        )

    assert resp.status_code == 202
    assert resp.media_type == "application/json"
    assert set(resp.content) == {"task_updated_at", "task_status"}
    mock_task.delay.assert_called_once()


@patch("superset.tasks.thumbnails.cache_dashboard_thumbnail")
@patch("superset.utils.screenshots.DashboardScreenshot")
async def test_dashboard_thumbnail_pending_returns_json_202(
    mock_ds: MagicMock,
    mock_task: MagicMock,
) -> None:
    """Dashboard: a queued thumbnail returns 202 + application/json (5 keys)."""
    mock_task.delay = MagicMock()
    inst = mock_ds.return_value
    inst.get_cache_key.return_value = "ck456"
    inst.cache.set = MagicMock()
    mock_ds.get_from_cache_key.return_value = None

    dashboard = MagicMock()
    dashboard.digest = "d"
    dashboard.id = 7
    dao = AsyncMock()
    # The handler now uses an access-scoped get_full_by_id_or_slug.
    dao.get_full_by_id_or_slug = AsyncMock(return_value=dashboard)

    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[]),
    ):
        resp = await _dashboard_thumbnail(
            DashboardController(owner=MagicMock()),
            pk=7,
            digest="d",
            dao=dao,
            state=_state({"THUMBNAILS": True}),
            current_user=MagicMock(),
            security_manager=AsyncMock(),
        )

    assert resp.status_code == 202
    assert resp.media_type == "application/json"
    assert set(resp.content) == {
        "cache_key",
        "dashboard_url",
        "image_url",
        "task_updated_at",
        "task_status",
    }
    mock_task.delay.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
