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

"""
Tests for screenshot exception handling in API endpoints.

Ported from the upstream Flask suite. The Liteset chart/dashboard
screenshot controllers serve the cached PNG bytes and return a Litestar
``Response(status_code=404)`` (rather than the FAB ``response_404()``)
when ``ScreenshotCachePayload.get_image()`` raises
``ScreenshotImageNotAvailableException``; the simulations below mirror
that behaviour without the Flask/FAB/werkzeug scaffolding.
"""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.exceptions import ScreenshotImageNotAvailableException
from superset.utils.screenshots import ScreenshotCachePayload, StatusValues


def _make_unavailable_payload():
    """A cache payload whose ``get_image()`` raises the not-available error."""
    payload = MagicMock()
    payload.status = StatusValues.UPDATED
    payload.get_image.side_effect = ScreenshotImageNotAvailableException()
    return payload


class TestScreenshotAPIExceptionHandling:
    """Test that API endpoints properly handle
    ScreenshotImageNotAvailableException."""

    @pytest.mark.asyncio
    async def test_dashboard_screenshot_api_handles_exception(self):
        """Dashboard screenshot API returns 404 when get_image raises exception.

        Ports the upstream ``DashboardRestApi`` test: the real Liteset
        ``DashboardController.screenshot`` handler catches
        ``ScreenshotImageNotAvailableException`` and returns a 404
        ``Response`` (the port's equivalent of FAB ``response_404()``).
        """
        from superset.controllers.dashboard import DashboardController

        api = DashboardController(owner=None)

        dao = MagicMock()
        dao.get_full_by_id_or_slug = AsyncMock(return_value=SimpleNamespace(id=1))
        state = SimpleNamespace(
            settings=SimpleNamespace(
                feature_flags={
                    "THUMBNAILS": True,
                    "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS": True,
                }
            )
        )
        request = MagicMock()
        request.query_params.get.return_value = "png"

        with (
            patch(
                "superset.db.filters.dashboard_access_filters",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "superset.utils.screenshots.DashboardScreenshot.get_from_cache_key",
                return_value=_make_unavailable_payload(),
            ),
        ):
            response = await DashboardController.screenshot.fn(
                api,
                pk=1,
                digest="digest",
                dao=dao,
                state=state,
                security_manager=MagicMock(),
                current_user=MagicMock(),
                request=request,
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_chart_screenshot_api_handles_exception(self):
        """Chart screenshot API returns 404 when get_image raises exception.

        Ports the upstream ``ChartRestApi`` test: the real Liteset
        ``ChartController.screenshot`` handler catches
        ``ScreenshotImageNotAvailableException`` and returns a 404
        ``Response`` (the port's equivalent of FAB ``response_404()``).
        """
        from superset.controllers.chart import ChartController

        api = ChartController(owner=None)

        dao = MagicMock()
        dao.find_all = AsyncMock(return_value=[SimpleNamespace(id=1)])
        state = SimpleNamespace(
            settings=SimpleNamespace(feature_flags={"THUMBNAILS": True})
        )

        with (
            patch(
                "superset.db.filters.chart_access_filters",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "superset.utils.screenshots.ChartScreenshot.get_from_cache_key",
                return_value=_make_unavailable_payload(),
            ),
        ):
            response = await ChartController.screenshot.fn(
                api,
                pk=1,
                digest="digest",
                dao=dao,
                state=state,
                security_manager=MagicMock(),
                current_user=MagicMock(),
            )

        assert response.status_code == 404

    def test_screenshot_api_handles_exception(self):
        """Screenshot serving returns 404 when get_image raises exception.

        Mirrors the chart/dashboard ``screenshot`` controllers, which catch
        ``ScreenshotImageNotAvailableException`` and return a 404 response
        instead of serving image bytes.
        """

        def serve_image(cache_payload):
            try:
                image = cache_payload.get_image()
            except ScreenshotImageNotAvailableException:
                return 404
            return image

        # Payload with no image raises -> 404
        payload_no_image = ScreenshotCachePayload()
        assert serve_image(payload_no_image) == 404

        # Payload with an image -> served bytes
        payload_with_image = ScreenshotCachePayload(image=b"test data")
        result = serve_image(payload_with_image)
        assert result is not None
        assert result.read() == b"test data"

    def test_screenshot_cache_payload_exception_has_correct_status(self):
        """Test that the ScreenshotImageNotAvailableException has status 404."""
        exception = ScreenshotImageNotAvailableException()
        # The port exposes ``status`` as a backward-compat alias of
        # ``status_code`` on SupersetException.
        assert exception.status == 404
        assert exception.status_code == 404

    def test_api_method_simulation_with_exception(self):
        """Simulate the API method behavior with exception handling."""

        def simulate_dashboard_screenshot_method(cache_payload):
            """Simulate the logic in dashboard screenshot methods."""
            try:
                image = cache_payload.get_image()
                return {"status": "success", "image": image}
            except ScreenshotImageNotAvailableException:
                return {"status": "404", "message": "Not Found"}

        # Test with payload that has image
        payload_with_image = ScreenshotCachePayload(image=b"test data")
        result = simulate_dashboard_screenshot_method(payload_with_image)
        assert result["status"] == "success"
        assert result["image"] is not None

        # Test with payload that has no image (should raise exception)
        payload_no_image = ScreenshotCachePayload()
        result = simulate_dashboard_screenshot_method(payload_no_image)
        assert result["status"] == "404"
        assert result["message"] == "Not Found"

    def test_api_method_simulation_with_image_stream(self):
        """Simulate the image-stream usage in API methods.

        The upstream test exercised werkzeug's ``FileWrapper``; the Liteset
        port serves the ``BytesIO`` returned by ``get_image()`` directly via a
        Litestar ``Response``/``Stream``, so the simulation streams the
        ``BytesIO`` and returns ``None`` on the not-available exception.
        """

        def simulate_api_file_response(cache_payload):
            """Simulate the image-stream logic in API methods."""
            try:
                image = cache_payload.get_image()
                return image
            except ScreenshotImageNotAvailableException:
                return None

        # Test with valid image
        payload_with_image = ScreenshotCachePayload(image=b"test data")
        result = simulate_api_file_response(payload_with_image)
        assert result is not None
        assert isinstance(result, BytesIO)
        assert result.read() == b"test data"

        # Test without image
        payload_no_image = ScreenshotCachePayload()
        result = simulate_api_file_response(payload_no_image)
        assert result is None
