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

"""URL helpers.

:func:`get_url_path` resolves view names (``Superset.dashboard``,
``ExploreView.root``, etc.) to concrete URL templates for the screenshot,
thumbnail, report, and Celery code paths.

The mapping is intentionally explicit (rather than reflective on the
Litestar router) because:

* Several names — ``Superset.dashboard``, ``Superset.slice``,
  ``Superset.dashboard_permalink``, ``ExploreView.root`` — point at
  legacy template-rendering routes that live on the legacy SPA, not on
  the Litestar API; their URLs are part of the public surface.
* ``ChartDataRestApi.get_data``, ``ChartRestApi.warm_up_cache``,
  ``ChartRestApi.screenshot``, ``DashboardRestApi.thumbnail``,
  ``DashboardRestApi.screenshot``, ``SecurityRestApi.csrf_token`` are
  Liteset REST endpoints; we hard-code their canonical paths to avoid
  importing the controllers (and their full DAO/middleware chain) just
  to compute a URL.

The remaining helpers (:func:`headless_url`, :func:`is_secure_url`,
:func:`modify_url_query`) are pure stdlib and ported verbatim.
"""

from __future__ import annotations

import functools
import urllib.parse
from typing import Any
from urllib.parse import urlparse


@functools.lru_cache(maxsize=1)
def _cached_settings() -> Any:
    """Return a process-wide cached :class:`SupersetSettings` instance.

    Imported lazily so this module is safe to import at module-load
    time (for example, from ``utils/screenshots.py``) without paying
    the env-scan / file I/O of building ``SupersetSettings``.
    """
    from superset.config import SupersetSettings

    return SupersetSettings()  # type: ignore[call-arg]


def _baseurl(user_friendly: bool) -> str:
    """Pull the base URL out of ``SupersetSettings``.

    Returns ``WEBDRIVER_BASEURL_USER_FRIENDLY`` or ``WEBDRIVER_BASEURL``
    directly, with no fallback between them.  Operators who want the
    user-friendly URL to mirror the headless one must set both keys
    explicitly.
    """
    settings = _cached_settings()
    if user_friendly:
        return str(getattr(settings, "webdriver_baseurl_user_friendly", "") or "")
    return str(getattr(settings, "webdriver_baseurl", "") or "")


def get_url_host(user_friendly: bool = False) -> str:
    """Return the configured ``WEBDRIVER_BASEURL`` (or the user-friendly
    variant when ``user_friendly=True``).

    Values come from :class:`SupersetSettings`.
    """
    return _baseurl(user_friendly)


def headless_url(path: str, user_friendly: bool = False) -> str:
    """Join ``path`` onto the configured base URL.

    Verbatim port of the original — ``urljoin`` semantics matter
    (relative ``path`` resolved against the base, absolute ``path``
    replaces it).
    """
    return urllib.parse.urljoin(get_url_host(user_friendly=user_friendly), path)


#
# Each entry maps a legacy upstream view name (used by the original
# ``get_url_path`` callers) to:
#
#   1. The set of kwargs whose values fill placeholders in the URL
#      template (``path_params``);
#   2. A format string for the URL itself.
#
# Any kwarg passed to :func:`get_url_path` that is *not* in the
# template's path-params set is treated as a query-string parameter,
# matching the way the upstream ``url_for`` appends unknown kwargs as a
# query string.
#
# Empty path_params (e.g. ``ChartRestApi.warm_up_cache``) means every
# kwarg is a query-string parameter.

_PathSpec = tuple[set[str], str]

_VIEW_TEMPLATES: dict[str, _PathSpec] = {
    # ``Superset.dashboard`` — legacy SPA dashboard view.
    "Superset.dashboard": (
        {"dashboard_id_or_slug"},
        "/superset/dashboard/{dashboard_id_or_slug}/",
    ),
    # ``Superset.slice`` — legacy SPA explore-view shortcut by slice id.
    "Superset.slice": ({"slice_id"}, "/superset/slice/{slice_id}/"),
    # ``Superset.dashboard_permalink`` — legacy SPA stateful dashboard link.
    "Superset.dashboard_permalink": (
        {"key"},
        "/superset/dashboard/p/{key}/",
    ),
    # ``Superset.welcome`` — landing page after login.
    "Superset.welcome": (set(), "/superset/welcome/"),
    # ``Superset.profile`` — current user's profile page.
    "Superset.profile": (set(), "/superset/profile/"),
    # ``Superset.explore`` — legacy SPA explore alias (legacy URL).
    "Superset.explore": (set(), "/explore/"),
    # ``Superset.filter`` — column-filter ajax endpoint (cascading filters).
    "Superset.filter": (
        {"datasource_type", "datasource_id", "column"},
        "/superset/filter/{datasource_type}/{datasource_id}/{column}/",
    ),
    # ``ExploreView.root`` — legacy SPA explore root (chart builder).
    "ExploreView.root": (set(), "/explore/"),
    # Liteset REST: chart data endpoint.
    "ChartDataRestApi.get_data": (
        {"pk"},
        "/api/v1/chart/{pk}/data/",
    ),
    # Liteset REST: chart cache warm-up.
    "ChartRestApi.warm_up_cache": (set(), "/api/v1/chart/warm_up_cache"),
    # Liteset REST: chart cache_screenshot (lazy-load digest).
    "ChartRestApi.cache_screenshot": (
        {"pk", "digest"},
        "/api/v1/chart/{pk}/cache_screenshot/{digest}/",
    ),
    # Liteset REST: chart screenshot fetch (cached image by digest).
    "ChartRestApi.screenshot": (
        {"pk", "digest"},
        "/api/v1/chart/{pk}/screenshot/{digest}/",
    ),
    # Liteset REST: dashboard thumbnail (kicks Celery task).
    "DashboardRestApi.thumbnail": (
        {"pk", "digest"},
        "/api/v1/dashboard/{pk}/thumbnail/{digest}/",
    ),
    # Liteset REST: dashboard screenshot fetch.
    "DashboardRestApi.screenshot": (
        {"pk", "digest"},
        "/api/v1/dashboard/{pk}/screenshot/{digest}/",
    ),
    # Liteset REST: chart thumbnail (kicks Celery task).
    "ChartRestApi.thumbnail": (
        {"pk", "digest"},
        "/api/v1/chart/{pk}/thumbnail/{digest}/",
    ),
    # Liteset REST: CSRF token endpoint.
    "SecurityRestApi.csrf_token": (set(), "/api/v1/security/csrf_token/"),
}


def _resolve_view_path(view: str, kwargs: dict[str, Any]) -> str:
    """Resolve ``view`` to a URL path, substituting path params and
    appending the rest as query-string parameters.

    Raises :class:`ValueError` for unknown views — more informative than
    the bare ``KeyError`` that ``dict[view]`` would raise, and matches
    the spirit of the upstream ``BuildError`` ("could not build url for
    endpoint").  Add the missing view to :data:`_VIEW_TEMPLATES` with
    its URL template.
    """
    spec = _VIEW_TEMPLATES.get(view)
    if spec is None:
        raise ValueError(
            f"Unknown view name {view!r}. Add it to "
            "superset.utils.urls._VIEW_TEMPLATES with its URL template."
        )
    path_param_names, template = spec

    # Pull path params out of kwargs; whatever's left becomes querystring.
    path_values: dict[str, Any] = {}
    query_values: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key in path_param_names:
            path_values[key] = value
        else:
            query_values[key] = value

    # Path-param substitution. Missing path params raise KeyError, which
    # is exactly what we want — the call-site bug surfaces immediately.
    path = template.format(**path_values)

    if query_values:
        # Use ``doseq=True`` so list-valued kwargs (rare but legal in
        # the upstream url_for) are encoded as repeated key=value pairs.
        path = f"{path}?{urllib.parse.urlencode(query_values, doseq=True)}"
    return path


def get_url_path(view: str, user_friendly: bool = False, **kwargs: Any) -> str:
    """Return a fully-qualified URL for ``view`` against the configured
    base URL.

    The original used the upstream ``url_for`` helper to resolve
    ``view`` against the in-process URL map (under
    ``app.test_request_context`` when no real request was bound).  In
    Liteset we resolve through the static :data:`_VIEW_TEMPLATES`
    mapping above — see this module's docstring for rationale.
    """
    return headless_url(_resolve_view_path(view, kwargs), user_friendly=user_friendly)


def modify_url_query(url: str, **kwargs: Any) -> str:
    """Replace or add parameters to a URL.

    Verbatim port of the original — pure stdlib, no legacy dependency.
    """
    parts = list(urllib.parse.urlsplit(url))
    params = urllib.parse.parse_qs(parts[3])
    for k, v in kwargs.items():
        if not isinstance(v, list):
            v = [v]
        params[k] = v

    parts[3] = "&".join(
        f"{k}={urllib.parse.quote(str(v[0]))}" for k, v in params.items()
    )
    return urllib.parse.urlunsplit(parts)


def is_secure_url(url: str) -> bool:
    """Validate that a URL uses HTTPS.

    :param url: The URL to validate.
    :return: ``True`` if the URL uses HTTPS, ``False`` otherwise.
    """
    parsed_url = urlparse(url)
    return parsed_url.scheme == "https"
