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

"""Screenshot / thumbnail computation utilities — port of
``superset_old/utils/screenshots.py`` to Liteset.

The original module reached into ``app.config`` and the
``thumbnail_cache`` LocalProxy (an upstream ``Cache`` instance
configured by ``THUMBNAIL_CACHE_CONFIG``).  In Liteset:

* All config reads go through :class:`SupersetSettings` (lazily cached
  via :func:`_cached_settings`, mirroring the pattern in
  :mod:`superset.utils.rls`).
* The thumbnail cache is sourced from
  :attr:`superset.cache.manager.CacheManager.sync_thumbnail_cache` —
  the *sync* sibling of the canonical async thumbnail cache.  Both
  point at the same Redis cluster (configured operator-side via
  ``THUMBNAIL_CACHE_CONFIG``) so the keyspace stays unified, but the
  sync client owns its own Redis connection pool so there is no risk
  of cross-event-loop awaits when a Celery worker or Selenium /
  Playwright thread reads from it.  The original Superset
  achieved the same effect with a single upstream ``Cache``
  instance shared across the request thread and Celery workers; we
  mirror that here with the sync/async sibling pair.
* The previous implementation used a sync-bridging adapter that ran
  the async cache from a worker thread via a sync→async bridge.
  That adapter has been replaced with the canonical
  ``cache_manager.sync_thumbnail_cache`` to fix a cross-event-loop
  bug: the async Redis client owns per-loop futures, and reaching it
  from a fresh ``asyncio.run`` on the worker thread (the deadlock-
  guard fall-through path of the bridge) created broken futures
  whenever Redis I/O actually ran.  The sync sibling slot avoids
  that entirely.

Synchronous I/O (Selenium, Playwright, PIL) is intentional: this
module is invoked from Celery tasks which run on a thread.  The
controllers that exercise these classes from the ASGI request loop
wrap each call in :func:`asyncio.to_thread`.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from enum import Enum
from io import BytesIO
from typing import Any, cast, TYPE_CHECKING, TypedDict

from superset.events import event_logger
from superset.exceptions import ScreenshotImageNotAvailableException
from superset.utils.feature_flags import feature_flag_manager
from superset.utils.hashing import md5_sha_from_dict
from superset.utils.urls import modify_url_query
from superset.utils.webdriver import (
    cached_settings as _cached_settings,
    ChartStandaloneMode,
    DashboardStandaloneMode,
    WebDriverPlaywright,
    WebDriverProxy,
    WebDriverSelenium,
    WindowSize,
)

logger = logging.getLogger(__name__)

DEFAULT_SCREENSHOT_WINDOW_SIZE: WindowSize = (800, 600)
DEFAULT_SCREENSHOT_THUMBNAIL_SIZE: WindowSize = (400, 300)
DEFAULT_CHART_WINDOW_SIZE: WindowSize = (800, 600)
DEFAULT_CHART_THUMBNAIL_SIZE: WindowSize = (800, 600)
DEFAULT_DASHBOARD_WINDOW_SIZE: WindowSize = (1600, 1200)
DEFAULT_DASHBOARD_THUMBNAIL_SIZE: WindowSize = (800, 600)

try:
    from PIL import Image
except ModuleNotFoundError:
    Image = None  # type: ignore[assignment]
    logger.info("No PIL installation found")

if TYPE_CHECKING:
    from superset.models.security import User


# ---------------------------------------------------------------------------
# Thumbnail cache — single source of truth (CacheManager.sync_thumbnail_cache)
# ---------------------------------------------------------------------------


def _get_thumbnail_cache() -> Any:
    """Return the canonical thumbnail cache (always
    :attr:`CacheManager.sync_thumbnail_cache`).

    Used by the module-level ``thumbnail_cache`` proxy and by
    :class:`BaseScreenshot` (via the ``cache`` class attribute).  The
    returned object exposes the sync ``get`` / ``set`` / ``delete``
    shape that the rest of this module expects.

    Resolution is lazy so importing this module from CLI / alembic
    contexts (where ``CacheManager.init_app`` has not run) returns a
    no-op :class:`NullSyncCacheManager` rather than raising.
    """
    # Late import — ``superset.extensions`` imports ``CacheManager``
    # which is small but we still want to avoid pulling it in at
    # module-load time (this file is sometimes imported from alembic /
    # CLI before extensions are wired).
    from superset.extensions import cache_manager

    return cache_manager.sync_thumbnail_cache


class _ThumbnailCacheProxy:
    """Lazy attribute-forwarder that delegates every access to the
    canonical :attr:`CacheManager.sync_thumbnail_cache` (resolved via
    :func:`_get_thumbnail_cache`).

    This proxy contains **no** caching logic of its own — it exists
    purely so that

    * ``from superset.utils.screenshots import thumbnail_cache`` works
      at module-load time (the canonical cache object isn't built until
      :meth:`CacheManager.init_app` runs at app startup);
    * the class-level ``BaseScreenshot.cache`` attribute (used as
      ``cls.cache.get(...)`` in classmethods) still resolves correctly
      from both instance and class access — a regular ``@property``
      wouldn't, since ``cls.cache`` would return the descriptor rather
      than the underlying cache.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(_get_thumbnail_cache(), name)

    def __repr__(self) -> str:
        return f"<thumbnail_cache proxy → {_get_thumbnail_cache()!r}>"


# Module-level handle for legacy callers that did
# ``from superset.utils.screenshots import thumbnail_cache``.
thumbnail_cache: Any = _ThumbnailCacheProxy()


# ---------------------------------------------------------------------------
# Cache payload
# ---------------------------------------------------------------------------


class StatusValues(Enum):
    PENDING = "Pending"
    COMPUTING = "Computing"
    UPDATED = "Updated"
    ERROR = "Error"


class ScreenshotCachePayloadType(TypedDict):
    image: str | None
    timestamp: str
    status: str


class ScreenshotCachePayload:
    def __init__(
        self,
        image: bytes | None = None,
        status: StatusValues = StatusValues.PENDING,
        timestamp: str = "",
    ) -> None:
        self._image = image
        self._timestamp = timestamp or datetime.now().isoformat()
        self.status = StatusValues.UPDATED if image else status

    @classmethod
    def from_dict(cls, payload: ScreenshotCachePayloadType) -> ScreenshotCachePayload:
        return cls(
            image=base64.b64decode(payload["image"]) if payload["image"] else None,
            status=StatusValues(payload["status"]),
            timestamp=payload["timestamp"],
        )

    def to_dict(self) -> ScreenshotCachePayloadType:
        return {
            "image": (
                base64.b64encode(self._image).decode("utf-8") if self._image else None
            ),
            "timestamp": self._timestamp,
            "status": self.status.value,
        }

    def update_timestamp(self) -> None:
        self._timestamp = datetime.now().isoformat()

    def pending(self) -> None:
        self.update_timestamp()
        self._image = None
        self.status = StatusValues.PENDING

    def computing(self) -> None:
        self.update_timestamp()
        self._image = None
        self.status = StatusValues.COMPUTING

    def update(self, image: bytes) -> None:
        self.update_timestamp()
        self.status = StatusValues.UPDATED
        self._image = image

    def error(self) -> None:
        self.update_timestamp()
        self.status = StatusValues.ERROR

    def get_image(self) -> BytesIO:
        if self._image is None:
            raise ScreenshotImageNotAvailableException()
        return BytesIO(cast(bytes, self._image))

    def get_timestamp(self) -> str:
        return self._timestamp

    def get_status(self) -> str:
        return self.status.value

    def is_error_cache_ttl_expired(self) -> bool:
        settings = _cached_settings()
        error_cache_ttl = getattr(settings, "thumbnail_error_cache_ttl", 86400)
        return (
            datetime.now() - datetime.fromisoformat(self.get_timestamp())
        ).total_seconds() > error_cache_ttl

    def is_computing_stale(self) -> bool:
        """Check if a COMPUTING status is stale (task likely failed or stuck)."""
        # Use the same TTL as error cache - if computing takes longer
        # than this, it's likely stuck and should be retried.
        settings = _cached_settings()
        computing_ttl = getattr(settings, "thumbnail_error_cache_ttl", 86400)
        return (
            datetime.now() - datetime.fromisoformat(self.get_timestamp())
        ).total_seconds() >= computing_ttl

    def should_trigger_task(self, force: bool = False) -> bool:
        return (
            force
            or self.status == StatusValues.PENDING
            or (self.status == StatusValues.ERROR and self.is_error_cache_ttl_expired())
            or (self.status == StatusValues.COMPUTING and self.is_computing_stale())
            or (self.status == StatusValues.UPDATED and self._image is None)
        )


# ---------------------------------------------------------------------------
# Screenshot classes
# ---------------------------------------------------------------------------


class BaseScreenshot:
    @property
    def driver_type(self) -> str:
        return getattr(_cached_settings(), "webdriver_type", "firefox")

    url: str
    digest: str | None
    screenshot: bytes | None
    thumbnail_type: str = ""
    element: str = ""
    window_size: WindowSize = DEFAULT_SCREENSHOT_WINDOW_SIZE
    thumb_size: WindowSize = DEFAULT_SCREENSHOT_THUMBNAIL_SIZE
    cache: Any = thumbnail_cache

    def __init__(self, url: str, digest: str | None) -> None:
        self.digest = digest
        self.url = url
        self.screenshot = None

    def driver(self, window_size: WindowSize | None = None) -> WebDriverProxy:
        window_size = window_size or self.window_size
        if feature_flag_manager.is_feature_enabled("PLAYWRIGHT_REPORTS_AND_THUMBNAILS"):
            return WebDriverPlaywright(self.driver_type, window_size)
        return WebDriverSelenium(self.driver_type, window_size)

    def get_screenshot(
        self, user: User, window_size: WindowSize | None = None
    ) -> bytes | None:
        driver = self.driver(window_size)
        self.screenshot = driver.get_screenshot(self.url, self.element, user)
        return self.screenshot

    def get_cache_key(
        self,
        window_size: bool | WindowSize | None = None,
        thumb_size: bool | WindowSize | None = None,
    ) -> str:
        window_size = window_size or self.window_size
        thumb_size = thumb_size or self.thumb_size
        args = {
            "thumbnail_type": self.thumbnail_type,
            "digest": self.digest,
            "type": "thumb",
            "window_size": window_size,
            "thumb_size": thumb_size,
        }
        return md5_sha_from_dict(args)

    def get_from_cache(
        self,
        window_size: WindowSize | None = None,
        thumb_size: WindowSize | None = None,
    ) -> ScreenshotCachePayload | None:
        cache_key = self.get_cache_key(window_size, thumb_size)
        return self.get_from_cache_key(cache_key)

    @classmethod
    def get_from_cache_key(cls, cache_key: str) -> ScreenshotCachePayload | None:
        logger.info("Attempting to get from cache: %s", cache_key)
        if payload := cls.cache.get(cache_key):
            # Initially, only bytes were stored. This was changed to
            # store an instance of ScreenshotCachePayload, but since it
            # can't be serialized in all backends it was further
            # changed to a dict of attributes.
            if isinstance(payload, bytes):
                payload = ScreenshotCachePayload(payload)
            elif isinstance(payload, ScreenshotCachePayload):
                pass
            elif isinstance(payload, dict):
                payload = cast(ScreenshotCachePayloadType, payload)
                payload = ScreenshotCachePayload.from_dict(payload)
            return payload
        logger.info("Failed at getting from cache: %s", cache_key)
        return None

    def compute_and_cache(
        self,
        force: bool,
        user: User | None = None,
        window_size: WindowSize | None = None,
        thumb_size: WindowSize | None = None,
        cache_key: str | None = None,
    ) -> None:
        """Compute the thumbnail and cache the result.

        :param force: Will force the computation even if it's already cached.
        :param user: If no user is given will use the current context.
        :param window_size: The window size from which to process the thumb.
        :param thumb_size: The final thumbnail size.
        :param cache_key: Optional precomputed cache key.
        """
        cache_key = cache_key or self.get_cache_key(window_size, thumb_size)
        cache_payload = self.get_from_cache_key(cache_key) or ScreenshotCachePayload()
        if not cache_payload.should_trigger_task(force=force):
            logger.info(
                "Skipping compute - already processed for thumbnail: %s",
                cache_key,
            )
            return

        window_size = window_size or self.window_size
        thumb_size = thumb_size or self.thumb_size
        logger.info("Processing url for thumbnail: %s", cache_key)
        cache_payload.computing()
        image: bytes | None = None
        # Assuming all sorts of things can go wrong with Selenium
        if user is None:
            from superset.utils.core import get_current_user

            user = get_current_user()
        if user is None:
            # Honour the original's "cache success or error" contract: a failed
            # capture must still reach a terminal ``Error`` state in the cache,
            # otherwise a polling client is stuck on ``Computing`` forever.
            # Persist the error payload before bailing out.
            logger.warning("compute_and_cache called without an authenticated user")
            cache_payload.error()
            self.cache.set(cache_key, cache_payload.to_dict())
            return
        try:
            logger.info("trying to generate screenshot")
            with event_logger.log_context(f"screenshot.compute.{self.thumbnail_type}"):
                image = self.get_screenshot(user=user, window_size=window_size)
        except Exception as ex:  # pylint: disable=broad-except
            logger.warning("Failed at generating thumbnail %s", ex, exc_info=True)
            cache_payload.error()
        if image and window_size != thumb_size:
            try:
                image = self.resize_image(image, thumb_size=thumb_size)
            except Exception as ex:  # pylint: disable=broad-except
                logger.warning("Failed at resizing thumbnail %s", ex, exc_info=True)
                cache_payload.error()
                image = None

        # Cache the result (success or error) to avoid immediate retries
        if image:
            with event_logger.log_context(f"screenshot.cache.{self.thumbnail_type}"):
                cache_payload.update(image)

        logger.info("Caching thumbnail: %s", cache_key)
        self.cache.set(cache_key, cache_payload.to_dict())
        logger.info("Updated thumbnail cache; Status: %s", cache_payload.get_status())
        return

    @classmethod
    def resize_image(
        cls,
        img_bytes: bytes,
        output: str = "png",
        thumb_size: WindowSize | None = None,
        crop: bool = True,
    ) -> bytes:
        if Image is None:  # pragma: no cover - guarded by import
            raise RuntimeError("PIL/Pillow is not installed; cannot resize screenshot.")
        thumb_size = thumb_size or cls.thumb_size
        img: Any = Image.open(BytesIO(img_bytes))
        logger.debug("Selenium image size: %s", str(img.size))
        if crop and img.size[1] != cls.window_size[1]:
            desired_ratio = float(cls.window_size[1]) / cls.window_size[0]
            desired_width = int(img.size[0] * desired_ratio)
            logger.debug("Cropping to: %s*%s", str(img.size[0]), str(desired_width))
            img = img.crop((0, 0, img.size[0], desired_width))
        logger.debug("Resizing to %s", str(thumb_size))
        img = img.resize(thumb_size, Image.Resampling.LANCZOS)
        new_img = BytesIO()
        if output != "png":
            img = img.convert("RGB")
        img.save(new_img, output)
        new_img.seek(0)
        return new_img.read()


class ChartScreenshot(BaseScreenshot):
    thumbnail_type: str = "chart"
    element: str = "chart-container"

    def __init__(
        self,
        url: str,
        digest: str | None,
        window_size: WindowSize | None = None,
        thumb_size: WindowSize | None = None,
    ) -> None:
        # Chart reports are in standalone="true" mode
        url = modify_url_query(
            url,
            standalone=ChartStandaloneMode.HIDE_NAV.value,
        )
        super().__init__(url, digest)
        self.window_size = window_size or DEFAULT_CHART_WINDOW_SIZE
        self.thumb_size = thumb_size or DEFAULT_CHART_THUMBNAIL_SIZE


class DashboardScreenshot(BaseScreenshot):
    thumbnail_type: str = "dashboard"
    element: str = "standalone"

    def __init__(
        self,
        url: str,
        digest: str | None,
        window_size: WindowSize | None = None,
        thumb_size: WindowSize | None = None,
    ) -> None:
        # per the element above, dashboard screenshots
        # should always capture in standalone
        url = modify_url_query(
            url,
            standalone=DashboardStandaloneMode.REPORT.value,
        )
        super().__init__(url, digest)
        self.window_size = window_size or DEFAULT_DASHBOARD_WINDOW_SIZE
        self.thumb_size = thumb_size or DEFAULT_DASHBOARD_THUMBNAIL_SIZE

    def get_cache_key(
        self,
        window_size: bool | WindowSize | None = None,
        thumb_size: bool | WindowSize | None = None,
        permalink_key: str | None = None,
    ) -> str:
        window_size = window_size or self.window_size
        thumb_size = thumb_size or self.thumb_size
        args = {
            "thumbnail_type": self.thumbnail_type,
            "digest": self.digest,
            "type": "thumb",
            "window_size": window_size,
            "thumb_size": thumb_size,
            "permalink_key": permalink_key,
        }
        return md5_sha_from_dict(args)
