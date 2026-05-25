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
"""Cache warming Celery tasks for Superset.

Ported 1:1 from the original ``superset/tasks/cache.py``.
Strategy classes query the database via :func:`superset.db.session.get_sync_session`
and :func:`fetch_url` sends HTTP PUT requests to warm chart caches.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, TypedDict, Union
from urllib import request
from urllib.error import URLError

from celery.beat import SchedulingError
from celery.utils.log import get_task_logger
from sqlalchemy import and_, func

from superset.tasks.celery_app import celery_app
from superset.tasks.exceptions import ExecutorNotFoundError, InvalidExecutorError
from superset.tasks.utils import get_executor

logger = get_task_logger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------


class CacheWarmupPayload(TypedDict, total=False):
    chart_id: int
    dashboard_id: int | None


class CacheWarmupTask(TypedDict):
    payload: CacheWarmupPayload
    username: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_task(
    chart: Any,
    dashboard: Any | None = None,
) -> CacheWarmupTask:
    """Return task for warming up a given chart/table cache.

    Ported from the original ``get_task``. Reads ``CACHE_WARMUP_EXECUTORS``
    from settings to determine which user should execute the warm-up.
    """
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    executors = settings.cache_warmup_executors

    payload: CacheWarmupPayload = {"chart_id": chart.id}
    if dashboard:
        payload["dashboard_id"] = dashboard.id

    username: str | None
    try:
        executor = get_executor(executors, chart)
        username = executor[1]
    except (ExecutorNotFoundError, InvalidExecutorError):
        username = None

    return {"payload": payload, "username": username}


def _get_warmup_url() -> str:
    """Build the chart warm-up cache URL from settings.

    The original used ``get_url_path("ChartRestApi.warm_up_cache")`` which
    relied on Flask's ``url_for()``. In Liteset we construct the URL
    directly from ``webdriver_baseurl`` + the known API path.
    """
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    base_url = settings.webdriver_baseurl.rstrip("/")
    return f"{base_url}/api/v1/chart/warm_up_cache"


def _is_secure_url(url: str) -> bool:
    """Check if the URL uses HTTPS."""
    return url.startswith("https://")


def _fetch_csrf_token(
    headers: dict[str, str], session_cookie_name: str = "session"
) -> dict[str, str]:
    """Fetch a CSRF token for API requests.

    1:1 port of ``superset_old/tasks/utils.py:fetch_csrf_token`` with two
    deployment-specific deviations:

    * URL — the original used ``get_url_path("SecurityRestApi.csrf_token")``
      (Flask ``url_for``); here we build it directly from ``WEBDRIVER_BASEURL``.
    * CSRF header name — the original sent ``X-CSRF-Token`` because Flask-WTF
      accepts both ``X-CSRFToken`` and ``X-CSRF-Token`` by default.  Liteset's
      :mod:`superset.middleware.csrf` reads a single header (``X-CSRFToken``,
      per ``CSRF_HEADER_NAME``), so we must send exactly that spelling or the
      warm-up PUT would be rejected.

    :param headers: headers to use in the request, including the session cookie
    :returns: a map of headers, including the session cookie and csrf token
    """
    import json

    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    base_url = settings.webdriver_baseurl.rstrip("/")
    url = f"{base_url}/api/v1/security/csrf_token/"

    logger.info("Fetching %s", url)
    req = request.Request(url, headers=headers, method="GET")  # noqa: S310
    with request.urlopen(req, timeout=600) as response:  # noqa: S310
        body = response.read().decode("utf-8")
        session_cookie: str | None = None
        cookie_headers = response.headers.get_all("set-cookie")
        if cookie_headers:
            for cookie in cookie_headers:
                cookie = cookie.split(";", 1)[0]
                name, value = cookie.split("=", 1)
                if name == session_cookie_name:
                    session_cookie = value
                    break

        if response.status == 200:
            data = json.loads(body)
            res = {"X-CSRFToken": data["result"]}
            if session_cookie is not None:
                res["Cookie"] = f"{session_cookie_name}={session_cookie}"
            return res

    logger.error("Error fetching CSRF token, status code: %s", response.status)
    return {}


def _get_auth_cookies(user: Any) -> dict[str, str]:
    """Generate authentication cookies for a user.

    Uses :class:`~superset.utils.machine_auth.MachineAuthProvider` — the same
    provider used by the thumbnail Celery tasks and the webdriver helper — so
    the minted cookie is accepted by
    :class:`~superset.middleware.auth.SupersetAuthMiddleware`.

    Falls back to the process-level factory singleton
    (:data:`superset.extensions.machine_auth_provider_factory`) when
    available; otherwise mints a settings-bound provider on the fly.
    """
    try:
        from superset.utils.machine_auth import MachineAuthProvider

        # Try the process-level singleton first (set up during startup).
        try:
            from superset.extensions import machine_auth_provider_factory

            provider = machine_auth_provider_factory.instance
            if provider is not None:
                return provider.get_auth_cookies(user)
        except (ImportError, AttributeError):
            pass

        # Fall back: mint a provider bound to the current settings.
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        provider = MachineAuthProvider()
        provider.bind_settings(settings)
        return provider.get_auth_cookies(user)
    except Exception:
        logger.warning("Failed to generate auth cookies", exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# Strategy classes
# ---------------------------------------------------------------------------


class Strategy:
    """A cache warm up strategy.

    Each strategy defines a ``get_tasks`` method that returns a list of
    tasks to send to the ``/api/v1/chart/warm_up_cache`` endpoint.

    Strategies can be configured in ``superset_config.py``::

        beat_schedule = {
            'cache-warmup-hourly': {
                'task': 'cache-warmup',
                'schedule': crontab(minute=1, hour='*'),
                'kwargs': {
                    'strategy_name': 'top_n_dashboards',
                    'top_n': 10,
                    'since': '7 days ago',
                },
            },
        }
    """

    def __init__(self) -> None:
        pass

    def get_tasks(self) -> list[CacheWarmupTask]:
        raise NotImplementedError("Subclasses must implement get_tasks!")


class DummyStrategy(Strategy):
    """Warm up all charts.

    This is a dummy strategy that will fetch all charts. Can be configured by::

        beat_schedule = {
            'cache-warmup-hourly': {
                'task': 'cache-warmup',
                'schedule': crontab(minute=1, hour='*'),
                'kwargs': {'strategy_name': 'dummy'},
            },
        }
    """

    name = "dummy"

    def get_tasks(self) -> list[CacheWarmupTask]:
        from superset.db.session import get_sync_session
        from superset.models.slice import Slice

        session = get_sync_session()
        try:
            return [get_task(chart) for chart in session.query(Slice).all()]
        finally:
            session.close()


class TopNDashboardsStrategy(Strategy):
    """Warm up charts in the top-n dashboards.

    Example config::

        beat_schedule = {
            'cache-warmup-hourly': {
                'task': 'cache-warmup',
                'schedule': crontab(minute=1, hour='*'),
                'kwargs': {
                    'strategy_name': 'top_n_dashboards',
                    'top_n': 5,
                    'since': '7 days ago',
                },
            },
        }
    """

    name = "top_n_dashboards"

    def __init__(self, top_n: int = 5, since: str = "7 days ago") -> None:
        super().__init__()
        self.top_n = top_n
        self.since = since

    def get_tasks(self) -> list[CacheWarmupTask]:
        from superset.db.session import get_sync_session
        from superset.models.core import Log
        from superset.models.dashboard import Dashboard
        from superset.utils.date import parse_human_datetime

        since_dt = parse_human_datetime(self.since) if self.since else None

        session = get_sync_session()
        try:
            records = (
                session.query(Log.dashboard_id, func.count(Log.dashboard_id))
                .filter(and_(Log.dashboard_id.isnot(None), Log.dttm >= since_dt))
                .group_by(Log.dashboard_id)
                .order_by(func.count(Log.dashboard_id).desc())
                .limit(self.top_n)
                .all()
            )
            dash_ids = [record.dashboard_id for record in records]
            dashboards = (
                session.query(Dashboard).filter(Dashboard.id.in_(dash_ids)).all()
            )

            return [
                get_task(chart, dashboard)
                for dashboard in dashboards
                for chart in dashboard.slices
            ]
        finally:
            session.close()


class DashboardTagsStrategy(Strategy):
    """Warm up charts in dashboards with custom tags.

    Example config::

        beat_schedule = {
            'cache-warmup-hourly': {
                'task': 'cache-warmup',
                'schedule': crontab(minute=1, hour='*'),
                'kwargs': {
                    'strategy_name': 'dashboard_tags',
                    'tags': ['core', 'warmup'],
                },
            },
        }
    """

    name = "dashboard_tags"

    def __init__(self, tags: Optional[list[str]] = None) -> None:
        super().__init__()
        self.tags = tags or []

    def get_tasks(self) -> list[CacheWarmupTask]:
        from superset.db.session import get_sync_session
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice
        from superset.models.tags import Tag, TaggedObject

        session = get_sync_session()
        try:
            tasks: list[CacheWarmupTask] = []
            tags = session.query(Tag).filter(Tag.name.in_(self.tags)).all()
            tag_ids = [tag.id for tag in tags]

            # Add dashboards that are tagged
            tagged_objects = (
                session.query(TaggedObject)
                .filter(
                    and_(
                        TaggedObject.object_type == "dashboard",
                        TaggedObject.tag_id.in_(tag_ids),
                    )
                )
                .all()
            )
            dash_ids = [tagged_object.object_id for tagged_object in tagged_objects]
            tagged_dashboards = session.query(Dashboard).filter(
                Dashboard.id.in_(dash_ids)
            )
            for dashboard in tagged_dashboards:
                for chart in dashboard.slices:
                    tasks.append(get_task(chart))

            # Add charts that are tagged
            tagged_objects = (
                session.query(TaggedObject)
                .filter(
                    and_(
                        TaggedObject.object_type == "chart",
                        TaggedObject.tag_id.in_(tag_ids),
                    )
                )
                .all()
            )
            chart_ids = [tagged_object.object_id for tagged_object in tagged_objects]
            tagged_charts = session.query(Slice).filter(Slice.id.in_(chart_ids))
            for chart in tagged_charts:
                tasks.append(get_task(chart))

            return tasks
        finally:
            session.close()


strategies = [DummyStrategy, TopNDashboardsStrategy, DashboardTagsStrategy]


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------


@celery_app.task(name="fetch_url")
def fetch_url(data: str, headers: dict[str, str]) -> dict[str, Any]:
    """Fetch a URL to warm up the chart cache.

    Sends an HTTP PUT request with the provided *data* payload and
    *headers* to the chart warm-up cache endpoint.
    Returns a dict indicating success or failure.

    The non-200 branch reports ``status_code`` as an ``int`` (the raw
    ``response.code``), matching the original ``fetch_url`` task exactly; the
    return type is therefore ``dict[str, Any]`` rather than ``dict[str, str]``.
    """
    result: dict[str, Any] = {}
    try:
        url = _get_warmup_url()

        if _is_secure_url(url):
            logger.info("URL '%s' is secure. Adding Referer header.", url)
            headers.update({"Referer": url})

        # Fetch CSRF token for API request
        headers.update(_fetch_csrf_token(headers))

        logger.info("Fetching %s with payload %s", url, data)
        req = request.Request(  # noqa: S310
            url, data=bytes(data, "utf-8"), headers=headers, method="PUT"
        )
        response = request.urlopen(req, timeout=600)  # noqa: S310
        logger.info(
            "Fetched %s with payload %s, status code: %s", url, data, response.code
        )
        if response.code == 200:
            result = {"success": data, "response": response.read().decode("utf-8")}
        else:
            result = {"error": data, "status_code": response.code}
            logger.error(
                "Error fetching %s with payload %s, status code: %s",
                url,
                data,
                response.code,
            )
    except URLError as err:
        logger.exception("Error warming up cache!")
        result = {"error": data, "exception": str(err)}
    return result


@celery_app.task(name="cache-warmup")
def cache_warmup(
    strategy_name: str, *args: Any, **kwargs: Any
) -> Union[dict[str, list[str]], str]:
    """Warm up cache.

    This task periodically hits charts to warm up the cache.
    Ported from the original ``cache_warmup`` task.
    """
    import json as json_module

    from superset.db.session import get_sync_session

    logger.info("Loading strategy")
    class_: type[Strategy] | None = None
    for class_ in strategies:
        if class_.name == strategy_name:  # type: ignore[attr-defined]
            break
    else:
        message = f"No strategy {strategy_name} found!"
        logger.error(message, exc_info=True)
        return message

    logger.info("Loading %s", class_.__name__)
    try:
        strategy = class_(*args, **kwargs)
        logger.info("Success!")
    except TypeError:
        message = "Error loading strategy!"
        logger.exception(message)
        return message

    results: dict[str, list[str]] = {"scheduled": [], "errors": []}

    # Load users for cookie generation via sync session
    session = get_sync_session()
    try:
        for task in strategy.get_tasks():
            username = task["username"]
            payload = json_module.dumps(task["payload"])
            if username:
                try:
                    from sqlalchemy import select

                    from superset.models.security import User

                    stmt = select(User).where(User.username == username)
                    user = session.execute(stmt).scalars().one_or_none()
                    if user is None:
                        logger.warning(
                            "User %s not found for payload: %s", username, payload
                        )
                        continue

                    cookies = _get_auth_cookies(user)
                    headers = {
                        "Cookie": f"session={cookies.get('session', '')}",
                        "Content-Type": "application/json",
                    }
                    logger.info("Scheduling %s", payload)
                    fetch_url.delay(payload, headers)
                    results["scheduled"].append(payload)
                except SchedulingError:
                    logger.exception(
                        "Error scheduling fetch_url for payload: %s", payload
                    )
                    results["errors"].append(payload)
            else:
                logger.warning("Executor not found for %s", payload)
    finally:
        session.close()

    return results
