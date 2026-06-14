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
"""Ported from ``tests/integration_tests/tasks/test_cache.py``.

Exercises :func:`superset.tasks.cache.fetch_url` — the chart warm-up cache
HTTP PUT helper. The upstream test patched ``fetch_csrf_token``,
``is_secure_url``, ``request.Request`` and ``request.urlopen`` and drove the
URL via ``app.config["WEBDRIVER_BASEURL"]``.

In the Liteset port those seams are renamed/private:

* ``is_secure_url`` -> :func:`superset.tasks.cache._is_secure_url`
* ``fetch_csrf_token`` -> :func:`superset.tasks.cache._fetch_csrf_token`
* the target URL is built by :func:`superset.tasks.cache._get_warmup_url`
  (which reads ``webdriver_baseurl`` from ``SupersetSettings`` and applies
  ``.rstrip("/")``), so we patch that seam directly instead of mutating
  ``app.config``.

The upstream parametrization covered four cases (HTTP/HTTPS x with/without a
trailing slash on the base URL) to verify both Referer handling and URL
normalization.  The ``fetch_url`` cases below preserve all four ids; because
``_get_warmup_url`` is mocked in those cases, the trailing-slash normalization
itself is exercised separately by ``test_get_warmup_url_normalizes_base_url``,
which drives the real ``_get_warmup_url`` through ``SupersetSettings``.
"""

from unittest import mock

import pytest


@pytest.mark.parametrize(
    "base_url, secure, expected_referer",
    [
        ("http://base-url", False, None),
        ("http://base-url/", False, None),
        (
            "https://base-url",
            True,
            "https://base-url/api/v1/chart/warm_up_cache",
        ),
        (
            "https://base-url/",
            True,
            "https://base-url/api/v1/chart/warm_up_cache",
        ),
    ],
    ids=[
        "Without trailing slash (HTTP)",
        "With trailing slash (HTTP)",
        "Without trailing slash (HTTPS)",
        "With trailing slash (HTTPS)",
    ],
)
@mock.patch("superset.tasks.cache._get_warmup_url")
@mock.patch("superset.tasks.cache._fetch_csrf_token")
@mock.patch("superset.tasks.cache.request.Request")
@mock.patch("superset.tasks.cache.request.urlopen")
@mock.patch("superset.tasks.cache._is_secure_url")
def test_fetch_url(
    mock_is_secure_url,
    mock_urlopen,
    mock_request_cls,
    mock_fetch_csrf_token,
    mock_get_warmup_url,
    base_url,
    secure,
    expected_referer,
):
    from superset.tasks.cache import fetch_url

    # ``_get_warmup_url`` rstrip("/")s the base URL, so the canonical warm-up
    # URL is the same regardless of a trailing slash on ``base_url``.
    warmup_url = f"{base_url.rstrip('/')}/api/v1/chart/warm_up_cache"
    mock_get_warmup_url.return_value = warmup_url

    mock_request = mock.MagicMock()
    mock_request_cls.return_value = mock_request

    mock_response = mock.MagicMock()
    mock_response.code = 200
    mock_response.read.return_value = b"ok"
    mock_urlopen.return_value = mock_response

    mock_is_secure_url.return_value = secure

    initial_headers = {"Cookie": "cookie", "key": "value"}
    csrf_headers = {"X-CSRFToken": "csrf_token"}
    mock_fetch_csrf_token.return_value = csrf_headers

    data = "data"
    data_encoded = b"data"

    # ``fetch_url`` mutates the headers dict in place (Referer + CSRF merge), so
    # the same object reference is passed through the CSRF seam and into
    # ``Request`` — pass it directly, mirroring the upstream test.
    headers = dict(initial_headers)
    result = fetch_url(data, headers)

    # Headers passed to Request are the initial headers, merged with the
    # (mocked) CSRF headers, plus the Referer header only when HTTPS.
    expected_headers = dict(initial_headers)
    if expected_referer:
        expected_headers["Referer"] = expected_referer
    expected_headers.update(csrf_headers)

    # The CSRF seam must be invoked exactly once with the headers object; the
    # recorded argument is the same dict that ``fetch_url`` mutates in place, so
    # it reflects the final merged headers (matching the upstream assertion).
    mock_fetch_csrf_token.assert_called_once_with(headers)
    assert headers == expected_headers

    mock_request_cls.assert_called_once_with(
        warmup_url,
        data=data_encoded,
        headers=expected_headers,
        method="PUT",
    )
    # assert the same Request object is used
    mock_urlopen.assert_called_once_with(mock_request, timeout=mock.ANY)

    assert data == result["success"]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://base-url",
        "http://base-url/",
        "https://base-url",
        "https://base-url/",
    ],
    ids=[
        "Without trailing slash (HTTP)",
        "With trailing slash (HTTP)",
        "Without trailing slash (HTTPS)",
        "With trailing slash (HTTPS)",
    ],
)
def test_get_warmup_url_normalizes_base_url(base_url):
    """``_get_warmup_url`` rstrip("/")s the base URL before appending the path.

    Restores the trailing-slash URL-normalization coverage that the upstream
    ``test_fetch_url`` parametrization exercised via ``WEBDRIVER_BASEURL``;
    here the real (un-mocked) ``_get_warmup_url`` is driven through
    ``SupersetSettings.webdriver_baseurl``.
    """
    from superset.tasks.cache import _get_warmup_url

    scheme = "https" if base_url.startswith("https") else "http"
    expected = f"{scheme}://base-url/api/v1/chart/warm_up_cache"

    with mock.patch("superset.config.SupersetSettings") as mock_settings_cls:
        mock_settings_cls.return_value.webdriver_baseurl = base_url
        assert _get_warmup_url() == expected
