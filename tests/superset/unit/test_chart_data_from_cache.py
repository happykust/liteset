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
"""Unit tests for the ``GET /api/v1/chart/data/<cache_key>`` port.

Covers the unit-testable seams of ``data_from_cache``:

* :func:`load_cached_query_context_form` -- the inverse of
  ``AsyncQueryContextProcessor._cache_set`` used to read the cached
  query-context form back out by ``cache_key``.
* ``AsyncQueryContextProcessor.get_payload`` now caches the form under the
  ``qc-`` key (``{"data": form_data}``) when ``cache_query_context=True``.
"""

from __future__ import annotations

import pickle  # noqa: S403 -- verifies _cache_set's pickle round-trip
from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.common.query_context import AsyncQueryContext
from superset.common.query_context_processor import (
    AsyncQueryContextProcessor,
    load_cached_query_context_form,
)
from superset.common.query_object import AsyncQueryObject

# ---------------------------------------------------------------------------
# load_cached_query_context_form
# ---------------------------------------------------------------------------


async def test_loader_returns_form_on_hit() -> None:
    """Pickled ``{"data": form}`` round-trips back to ``form``."""
    form = {"datasource": {"id": 7, "type": "table"}, "queries": [{"metrics": []}]}
    cache_manager = MagicMock()
    cache_manager.get = AsyncMock(
        return_value=pickle.dumps({"data": form}, protocol=pickle.HIGHEST_PROTOCOL)
    )

    result = await load_cached_query_context_form(cache_manager, "qc-abc")

    assert result == form
    cache_manager.get.assert_awaited_once_with("qc-abc")


async def test_loader_returns_none_on_miss() -> None:
    """A cache miss (``get`` returns ``None``) yields ``None``."""
    cache_manager = MagicMock()
    cache_manager.get = AsyncMock(return_value=None)

    result = await load_cached_query_context_form(cache_manager, "qc-missing")

    assert result is None


async def test_loader_returns_none_for_no_cache_manager() -> None:
    assert await load_cached_query_context_form(None, "qc-abc") is None


async def test_loader_returns_none_for_empty_key() -> None:
    cache_manager = MagicMock()
    cache_manager.get = AsyncMock(return_value=b"x")
    assert await load_cached_query_context_form(cache_manager, "") is None


async def test_loader_handles_sync_get() -> None:
    """A synchronous ``.get`` (non-awaitable) is supported too."""
    form = {"datasource": {"id": 1, "type": "table"}}
    cache_manager = MagicMock()
    cache_manager.get = MagicMock(
        return_value=pickle.dumps({"data": form}, protocol=pickle.HIGHEST_PROTOCOL)
    )

    result = await load_cached_query_context_form(cache_manager, "qc-sync")

    assert result == form


async def test_loader_returns_none_on_unpickleable_bytes() -> None:
    cache_manager = MagicMock()
    cache_manager.get = AsyncMock(return_value=b"not-a-pickle")
    assert await load_cached_query_context_form(cache_manager, "qc-bad") is None


async def test_loader_returns_none_when_data_key_missing() -> None:
    cache_manager = MagicMock()
    cache_manager.get = AsyncMock(
        return_value=pickle.dumps({"no_data": 1}, protocol=pickle.HIGHEST_PROTOCOL)
    )
    assert await load_cached_query_context_form(cache_manager, "qc-shape") is None


async def test_loader_get_failure_returns_none() -> None:
    cache_manager = MagicMock()
    cache_manager.get = AsyncMock(side_effect=RuntimeError("redis down"))
    assert await load_cached_query_context_form(cache_manager, "qc-err") is None


# ---------------------------------------------------------------------------
# get_payload caches the query-context form under qc-<hash>
# ---------------------------------------------------------------------------


def _make_processor() -> tuple[AsyncQueryContextProcessor, dict]:
    form_data = {
        "datasource": {"id": 42, "type": "table"},
        "queries": [{"metrics": ["count"]}],
        "result_type": "full",
        "result_format": "json",
    }
    datasource = MagicMock()
    datasource.uid = "42__table"
    # Make the cache-timeout chain fall through to cache_default_timeout.
    datasource.cache_timeout = None
    datasource.database.cache_timeout = None
    qctx = AsyncQueryContext(
        datasource=datasource,
        queries=[AsyncQueryObject(datasource={"id": 42, "type": "table"})],
        form_data=form_data,
        result_type="full",
        result_format="json",
    )
    settings = MagicMock()
    settings.cache_default_timeout = 3600
    settings.data_cache_config = {}
    proc = AsyncQueryContextProcessor(
        datasource=datasource,
        settings=settings,
        security_manager=MagicMock(),
        cache_manager=MagicMock(),  # non-None so the cache branch fires
        query_context=qctx,
    )
    return proc, form_data


async def test_get_payload_caches_form_when_cache_query_context() -> None:
    """``cache_query_context=True`` stores ``{"data": form_data}`` under qc-key."""
    proc, form_data = _make_processor()
    # Stub out the heavy bits so the test stays a pure unit test.
    proc._ensure_totals_available = AsyncMock()  # type: ignore[method-assign]
    proc.get_df_payload = AsyncMock(  # type: ignore[method-assign]
        return_value={"data": [], "df": None}
    )
    proc._cache_set = AsyncMock()  # type: ignore[method-assign]

    result = await proc.get_payload(
        proc._query_context.queries,
        cache_query_context=True,
    )

    assert "cache_key" in result
    cache_key = result["cache_key"]
    assert cache_key.startswith("qc-")

    proc._cache_set.assert_awaited_once()
    args = proc._cache_set.await_args.args
    # _cache_set(key, value, timeout)
    assert args[0] == cache_key
    assert args[1] == {"data": form_data}
    assert args[2] == 3600  # cache_default_timeout via _get_cache_timeout


async def test_get_payload_skips_form_cache_when_not_requested() -> None:
    """No ``cache_query_context`` -> no form caching and no cache_key."""
    proc, _ = _make_processor()
    proc._ensure_totals_available = AsyncMock()  # type: ignore[method-assign]
    proc.get_df_payload = AsyncMock(  # type: ignore[method-assign]
        return_value={"data": [], "df": None}
    )
    proc._cache_set = AsyncMock()  # type: ignore[method-assign]

    result = await proc.get_payload(
        proc._query_context.queries,
        cache_query_context=False,
    )

    assert "cache_key" not in result
    proc._cache_set.assert_not_awaited()


async def test_get_payload_skips_form_cache_without_cache_manager() -> None:
    """No cache manager -> no form caching even with cache_query_context=True."""
    proc, _ = _make_processor()
    proc._cache_manager = None
    proc._ensure_totals_available = AsyncMock()  # type: ignore[method-assign]
    proc.get_df_payload = AsyncMock(  # type: ignore[method-assign]
        return_value={"data": [], "df": None}
    )

    result = await proc.get_payload(
        proc._query_context.queries,
        cache_query_context=True,
    )

    assert "cache_key" not in result


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
