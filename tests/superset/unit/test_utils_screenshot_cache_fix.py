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
Tests for screenshot cache bug fixes:
1. Cache only saved when image generation succeeds
2. Recompute stale COMPUTING tasks and UPDATED without image

``thumbnail_error_cache_ttl`` is read from :class:`SupersetSettings`
(via the module-level ``_cached_settings`` re-export) rather than
``flask current_app.config``, so the TTL is injected by patching
``superset.utils.screenshots._cached_settings``.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from superset.utils.screenshots import (
    BaseScreenshot,
    ScreenshotCachePayload,
    StatusValues,
)

BASE_SCREENSHOT_PATH = "superset.utils.screenshots.BaseScreenshot"
SETTINGS_PATH = "superset.utils.screenshots._cached_settings"


class MockCache:
    def __init__(self):
        self._cache = {}

    def set(self, key, value):
        self._cache[key] = value

    def get(self, key):
        return self._cache.get(key)

    def clear(self):
        self._cache.clear()


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = 1
    return user


@pytest.fixture
def screenshot_obj():
    url = "http://example.com"
    digest = "sample_digest"
    return BaseScreenshot(url, digest)


@pytest.fixture
def ttl_300(mocker: MockerFixture):
    """Patch the cached settings so the error/computing TTL is 300s.

    Replaces the upstream ``@patch("superset.utils.screenshots.app")`` +
    ``mock_app.config = {"THUMBNAIL_ERROR_CACHE_TTL": 300}`` pattern; the
    Liteset port reads ``thumbnail_error_cache_ttl`` off the settings object.
    """
    mocker.patch(
        SETTINGS_PATH,
        return_value=SimpleNamespace(thumbnail_error_cache_ttl=300),
    )


class TestCacheOnlyOnSuccess:
    def _setup_mocks(self, mocker: MockerFixture, screenshot_obj):
        mocker.patch(BASE_SCREENSHOT_PATH + ".get_from_cache_key", return_value=None)
        get_screenshot = mocker.patch(
            BASE_SCREENSHOT_PATH + ".get_screenshot", return_value=b"image_data"
        )
        # Mock resize_image to avoid PIL errors with fake image data
        mocker.patch(
            BASE_SCREENSHOT_PATH + ".resize_image", return_value=b"resized_image_data"
        )
        BaseScreenshot.cache = MockCache()
        return get_screenshot

    def test_cache_error_status_when_screenshot_fails(
        self, mocker: MockerFixture, screenshot_obj, mock_user
    ):
        mocker.patch(BASE_SCREENSHOT_PATH + ".get_from_cache_key", return_value=None)
        get_screenshot = mocker.patch(
            BASE_SCREENSHOT_PATH + ".get_screenshot",
            side_effect=Exception("Screenshot failed"),
        )
        BaseScreenshot.cache = MockCache()

        screenshot_obj.compute_and_cache(user=mock_user, force=True)

        get_screenshot.assert_called_once()

        # ERROR status is cached to prevent immediate retries.
        cache_key = screenshot_obj.get_cache_key()
        cached_value = BaseScreenshot.cache.get(cache_key)
        assert cached_value is not None
        assert cached_value["status"] == "Error"
        assert cached_value.get("image") is None

    def test_cache_error_status_when_resize_fails(
        self, mocker: MockerFixture, screenshot_obj, mock_user
    ):
        self._setup_mocks(mocker, screenshot_obj)
        mocker.patch(
            BASE_SCREENSHOT_PATH + ".resize_image",
            side_effect=Exception("Resize failed"),
        )

        # Different window and thumb sizes trigger the resize path.
        screenshot_obj.compute_and_cache(
            user=mock_user, force=True, window_size=(800, 600), thumb_size=(400, 300)
        )

        # ERROR status is cached to prevent immediate retries.
        cache_key = screenshot_obj.get_cache_key()
        cached_value = BaseScreenshot.cache.get(cache_key)
        assert cached_value is not None
        assert cached_value["status"] == "Error"
        assert cached_value.get("image") is None

    def test_cache_saved_only_when_image_generated(
        self, mocker: MockerFixture, screenshot_obj, mock_user
    ):
        self._setup_mocks(mocker, screenshot_obj)

        screenshot_obj.compute_and_cache(user=mock_user, force=True)

        cache_key = screenshot_obj.get_cache_key()
        cached_value = BaseScreenshot.cache.get(cache_key)
        assert cached_value is not None
        assert cached_value["status"] == "Updated"
        assert cached_value["image"] is not None

    def test_no_intermediate_cache_during_computing(
        self, mocker: MockerFixture, screenshot_obj, mock_user
    ):
        mocker.patch(BASE_SCREENSHOT_PATH + ".get_from_cache_key", return_value=None)
        BaseScreenshot.cache = MockCache()

        def check_cache_during_screenshot(*args, **kwargs):
            # In COMPUTING state the cache must not be set yet.
            cache_key = screenshot_obj.get_cache_key()
            cached_value = BaseScreenshot.cache.get(cache_key)
            assert cached_value is None, (
                "Cache should not be saved during COMPUTING state"
            )
            return b"image_data"

        mocker.patch(
            BASE_SCREENSHOT_PATH + ".get_screenshot",
            side_effect=check_cache_during_screenshot,
        )
        mocker.patch(
            BASE_SCREENSHOT_PATH + ".resize_image", return_value=b"resized_image_data"
        )

        screenshot_obj.compute_and_cache(user=mock_user, force=True)

        cache_key = screenshot_obj.get_cache_key()
        cached_value = BaseScreenshot.cache.get(cache_key)
        assert cached_value is not None
        assert cached_value["status"] == "Updated"


class TestShouldTriggerTask:
    def test_trigger_on_stale_computing_status(self, ttl_300):
        # COMPUTING from 400s ago (stale at ttl=300).
        old_timestamp = (datetime.now() - timedelta(seconds=400)).isoformat()
        payload = ScreenshotCachePayload(
            status=StatusValues.COMPUTING, timestamp=old_timestamp
        )

        assert payload.should_trigger_task(force=False) is True

    def test_no_trigger_on_fresh_computing_status(self, ttl_300):
        # COMPUTING from 100s ago (still fresh at ttl=300).
        fresh_timestamp = (datetime.now() - timedelta(seconds=100)).isoformat()
        payload = ScreenshotCachePayload(
            status=StatusValues.COMPUTING, timestamp=fresh_timestamp
        )

        assert payload.should_trigger_task(force=False) is False

    def test_trigger_on_updated_without_image(self):
        # Simulates the bug where cache was saved UPDATED but without an image.
        payload = ScreenshotCachePayload(image=None, status=StatusValues.UPDATED)

        assert payload.should_trigger_task(force=False) is True

    def test_no_trigger_on_updated_with_image(self):
        payload = ScreenshotCachePayload(image=b"valid_image_data")

        assert payload.should_trigger_task(force=False) is False

    def test_trigger_on_pending_status(self):
        payload = ScreenshotCachePayload(status=StatusValues.PENDING)

        assert payload.should_trigger_task(force=False) is True

    def test_trigger_on_expired_error(self, ttl_300):
        # ERROR from 400s ago (expired at ttl=300).
        old_timestamp = (datetime.now() - timedelta(seconds=400)).isoformat()
        payload = ScreenshotCachePayload(
            status=StatusValues.ERROR, timestamp=old_timestamp
        )

        assert payload.should_trigger_task(force=False) is True

    def test_no_trigger_on_fresh_error(self, ttl_300):
        # ERROR from 100s ago (still fresh at ttl=300).
        fresh_timestamp = (datetime.now() - timedelta(seconds=100)).isoformat()
        payload = ScreenshotCachePayload(
            status=StatusValues.ERROR, timestamp=fresh_timestamp
        )

        assert payload.should_trigger_task(force=False) is False

    def test_force_always_triggers(self):
        # UPDATED + image normally would not trigger.
        payload_updated = ScreenshotCachePayload(image=b"image_data")
        assert payload_updated.should_trigger_task(force=True) is True

        # Fresh COMPUTING normally would not trigger.
        payload_computing = ScreenshotCachePayload(status=StatusValues.COMPUTING)
        assert payload_computing.should_trigger_task(force=True) is True


class TestIsComputingStale:
    def test_computing_is_stale(self, ttl_300):
        # 400s ago, ttl=300.
        old_timestamp = (datetime.now() - timedelta(seconds=400)).isoformat()
        payload = ScreenshotCachePayload(
            status=StatusValues.COMPUTING, timestamp=old_timestamp
        )

        assert payload.is_computing_stale() is True

    def test_computing_is_not_stale(self, ttl_300):
        # 100s ago, ttl=300.
        fresh_timestamp = (datetime.now() - timedelta(seconds=100)).isoformat()
        payload = ScreenshotCachePayload(
            status=StatusValues.COMPUTING, timestamp=fresh_timestamp
        )

        assert payload.is_computing_stale() is False

    def test_computing_exactly_at_ttl(self, ttl_300):
        exact_timestamp = (datetime.now() - timedelta(seconds=300)).isoformat()
        payload = ScreenshotCachePayload(
            status=StatusValues.COMPUTING, timestamp=exact_timestamp
        )

        # At exactly TTL the entry is stale (>= TTL).
        assert payload.is_computing_stale() is True

    def test_computing_just_past_ttl(self, ttl_300):
        past_ttl_timestamp = (datetime.now() - timedelta(seconds=301)).isoformat()
        payload = ScreenshotCachePayload(
            status=StatusValues.COMPUTING, timestamp=past_ttl_timestamp
        )

        assert payload.is_computing_stale() is True


class TestIntegrationCacheBugFix:
    def test_failed_screenshot_does_not_pollute_cache(
        self, mocker: MockerFixture, screenshot_obj, mock_user
    ):
        # A failed screenshot must cache ERROR status to prevent immediate
        # retries, not leave a corrupted UPDATED-with-image=None entry.
        mocker.patch(
            BASE_SCREENSHOT_PATH + ".get_screenshot",
            side_effect=Exception("Network error"),
        )
        BaseScreenshot.cache = MockCache()

        screenshot_obj.compute_and_cache(user=mock_user, force=True)

        cache_key = screenshot_obj.get_cache_key()
        cached_value = BaseScreenshot.cache.get(cache_key)
        assert cached_value is not None
        assert cached_value["status"] == "Error"
        assert cached_value.get("image") is None

        # A fresh error must not re-trigger the task immediately.
        cached_payload = screenshot_obj.get_from_cache_key(cache_key)
        assert cached_payload is not None
        assert cached_payload.should_trigger_task(force=False) is False

    def test_stale_computing_triggers_retry(
        self, ttl_300, mocker: MockerFixture, screenshot_obj, mock_user
    ):
        # Stale COMPUTING must trigger a retry to recover from stuck tasks.
        BaseScreenshot.cache = MockCache()

        old_timestamp = (datetime.now() - timedelta(seconds=400)).isoformat()
        stale_payload = ScreenshotCachePayload(
            status=StatusValues.COMPUTING, timestamp=old_timestamp
        )
        cache_key = screenshot_obj.get_cache_key()
        BaseScreenshot.cache.set(cache_key, stale_payload.to_dict())

        mocker.patch(
            BASE_SCREENSHOT_PATH + ".get_screenshot", return_value=b"recovered_image"
        )
        mocker.patch(
            BASE_SCREENSHOT_PATH + ".resize_image", return_value=b"resized_image"
        )

        assert stale_payload.should_trigger_task() is True

        screenshot_obj.compute_and_cache(user=mock_user, force=False)

        cached_value = BaseScreenshot.cache.get(cache_key)
        assert cached_value is not None
        assert cached_value["status"] == "Updated"
        assert cached_value["image"] is not None
