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
"""Tests for ExploreController."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.controllers.explore import ExploreController
from superset.exceptions import ObjectNotFoundError


def _explore_sm() -> MagicMock:
    """Security-manager mock whose datasource access check is awaitable +
    permissive (get_explore now enforces datasource access; denial is covered
    by the live RBAC probe / test_controller_rbac_access)."""
    sm = MagicMock()
    sm.raise_for_access = AsyncMock()
    return sm


# ---------------------------------------------------------------------------
# Controller metadata
# ---------------------------------------------------------------------------


def test_controller_path():
    assert ExploreController.path == "/api/v1/explore"


def test_controller_tags():
    assert ExploreController.tags == ["Explore"]


# ---------------------------------------------------------------------------
# Handler logic tests (call underlying fn directly)
# ---------------------------------------------------------------------------


async def test_get_explore_empty():
    request = MagicMock()
    request.query_params = {}
    chart_dao = AsyncMock()
    dataset_dao = AsyncMock()
    kv_dao = AsyncMock()

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=kv_dao,
        query_dao=AsyncMock(),
        security_manager=AsyncMock(),
        current_user=MagicMock(),
        session=AsyncMock(),
    )
    # On the empty-state path the handler fills form_data with explore
    # defaults (datasource/adhoc_filters/applied_time_extras/url_params) and
    # returns a "[Missing Dataset]" placeholder, matching original
    # GetExploreCommand.
    form_data = result["result"]["form_data"]
    assert form_data["adhoc_filters"] == []
    assert form_data["url_params"] == {}
    assert result["result"]["slice"] is None
    assert result["result"]["dataset"]["name"] == "[Missing Dataset]"
    assert result["result"]["message"] is None


async def test_get_explore_with_form_data_key(monkeypatch):
    request = MagicMock()
    request.query_params = {"form_data_key": "my-key"}
    chart_dao = AsyncMock()
    dataset_dao = AsyncMock()
    # The cache-slot entry envelope: owner/datasource_id/chart_id metadata +
    # form_data payload — 1:1 with the shape written by
    # explore_form_data.py:338-346.  The form_data_key branch reads through
    # ``cache_manager.explore_form_data_cache`` (same slot the
    # explore_form_data endpoints write to), NOT the kv_dao.
    fake_cache = AsyncMock()
    fake_cache.get.return_value = {
        "datasource_id": 1,
        "datasource_type": "table",
        "chart_id": None,
        "form_data": json.dumps({"viz_type": "bar"}),
    }
    monkeypatch.setattr(
        "superset.controllers.explore_form_data._form_data_cache",
        lambda: fake_cache,
    )

    # check_access enforces datasource/chart access on the cached form_data
    # (1:1 with GetFormDataCommand.run() in superset_old). Patch it to succeed
    # so this test exercises the success path without requiring real DB objects.
    monkeypatch.setattr(
        "superset.commands.explore_form_data.utils.check_access",
        AsyncMock(return_value=True),
    )

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=AsyncMock(),
        query_dao=AsyncMock(),
        security_manager=AsyncMock(),
        current_user=MagicMock(),
        session=AsyncMock(),
    )
    fake_cache.get.assert_awaited_once_with("my-key")
    # The handler applies upstream transforms (convert_legacy_filters_into_adhoc
    # / merge_extra_filters / merge_request_params) on top of the loaded
    # form_data, so assert the relevant value rather than an exact dict.
    assert result["result"]["form_data"]["viz_type"] == "bar"


async def test_get_explore_with_slice_id():
    request = MagicMock()
    request.query_params = {"slice_id": "10"}
    chart = MagicMock()
    chart.id = 10
    chart.slice_name = "My Chart"
    chart.viz_type = "table"
    chart.params = json.dumps({"granularity": "day"})
    # ``Slice.form_data`` is a real property on the model; with a MagicMock we
    # supply the migrated form_data dict the handler merges into the response.
    chart.form_data = {"granularity": "day"}
    # Iterable relationships eagerly read by the handler.
    chart.owners = []
    chart.dashboards = []
    chart.created_by = None
    chart.changed_by = None
    chart.changed_on = None
    chart.created_on = None
    chart.datasource_id = None

    chart_dao = AsyncMock()
    # The controller resolves the slice via find_by_id_with_options (eager-load),
    # not find_by_id.
    chart_dao.find_by_id_with_options.return_value = chart
    # The controller also calls find_by_id in the datasource fallback path;
    # return the same chart so datasource_id=None propagates and no dataset
    # lookup is triggered (avoiding the MagicMock auto-attribute default_endpoint
    # from a fresh AsyncMock return value triggering a spurious 302 redirect).
    chart_dao.find_by_id.return_value = chart
    dataset_dao = AsyncMock()
    kv_dao = AsyncMock()

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=kv_dao,
        query_dao=AsyncMock(),
        security_manager=AsyncMock(),
        current_user=MagicMock(),
        session=AsyncMock(),
    )
    assert result["result"]["slice"]["slice_id"] == 10
    assert result["result"]["form_data"]["granularity"] == "day"


async def test_get_explore_chart_not_found():
    request = MagicMock()
    request.query_params = {"slice_id": "999"}
    chart_dao = AsyncMock()
    # The controller resolves the slice via find_by_id_with_options.
    chart_dao.find_by_id_with_options.return_value = None
    chart_dao.find_by_id.return_value = None
    dataset_dao = AsyncMock()
    kv_dao = AsyncMock()

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=kv_dao,
        query_dao=AsyncMock(),
        security_manager=AsyncMock(),
        current_user=MagicMock(),
        session=AsyncMock(),
    )
    assert result["result"]["slice"] is None
    # Original GetExploreCommand never sets a "not found" message for a missing
    # slice (superset_old/commands/explore/get.py:63 initialises message=None
    # and no branch sets it when slc is None); the fix correctly mirrors that.
    assert result["result"]["message"] is None


# ---------------------------------------------------------------------------
# Permalink resolution (regression: explore permalink never resolved → 404)
# ---------------------------------------------------------------------------


async def test_get_explore_with_permalink_key_round_trip(monkeypatch):
    """A permalink_key (hashids string) must be decoded → int id, looked up in
    the EXPLORE_PERMALINK resource, and its ``state.formData`` returned.

    1:1 with superset_old/commands/explore/get.py:64-73 +
    superset_old/commands/explore/permalink/get.py — and consistent with how
    CreateExplorePermalinkCommand (superset.controllers.explore_permalink)
    actually stores the payload: ``{..., "state": {"formData": {...},
    "urlParams": [...]}}`` keyed by an auto-generated integer id, with the int
    encoded into the URL string via ``encode_permalink_key``.
    """
    request = MagicMock()
    request.query_params = {"permalink_key": "qQ8Rb3X"}
    chart_dao = AsyncMock()
    dataset_dao = AsyncMock()
    kv_dao = AsyncMock()

    # Stored permalink payload (the WRITE shape) keyed by int id 42.
    stored = {
        "chartId": None,
        "datasourceId": 1,
        "datasourceType": "table",
        "datasource": "1__table",
        "state": {
            "formData": {"viz_type": "bar", "datasource": "1__table"},
            "urlParams": [["foo", "bar"]],
        },
    }

    # Decode the hashid string → the int id used on write.
    monkeypatch.setattr(
        "superset.key_value.shared_entries.get_permalink_salt",
        AsyncMock(return_value="abc"),
    )
    monkeypatch.setattr(
        "superset.key_value.utils.decode_permalink_id",
        lambda key, salt: 42,
    )

    captured: dict[str, object] = {}

    class _FakeDAO:
        def __init__(self, session):
            pass

        async def get_value_by_key(self, resource, key):
            captured["resource"] = resource
            captured["key"] = key
            return stored

    monkeypatch.setattr("superset.db.daos.key_value.AsyncKeyValueDAO", _FakeDAO)
    # The permalink branch now enforces check_chart_access (1:1 with
    # GetExplorePermalinkCommand); this test covers resolution, not RBAC.
    monkeypatch.setattr(
        "superset.commands.explore_form_data.utils.check_access",
        AsyncMock(return_value=True),
    )

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=kv_dao,
        query_dao=AsyncMock(),
        security_manager=_explore_sm(),
        current_user=MagicMock(),
        session=AsyncMock(),
    )

    # Looked up under the correct resource, by the decoded INTEGER key.
    assert captured["resource"] == "explore_permalink"
    assert captured["key"] == 42
    # The stored state.formData is resolved into the response form_data, and
    # state.urlParams is merged into form_data["url_params"] (1:1 with original).
    form_data = result["result"]["form_data"]
    assert form_data["viz_type"] == "bar"
    # state.urlParams is merged into form_data["url_params"]; the handler also
    # folds the request query params in downstream, so assert membership.
    assert form_data["url_params"]["foo"] == "bar"


async def test_get_explore_with_bad_permalink_key_404(monkeypatch):
    """A bogus/expired permalink_key → 404 (ExplorePermalinkGetFailedError),
    1:1 with GetExplorePermalinkCommand. Two failure modes: undecodable key,
    and decodable key with no matching KV row."""
    request = MagicMock()
    request.query_params = {"permalink_key": "not-a-real-key"}

    monkeypatch.setattr(
        "superset.key_value.shared_entries.get_permalink_salt",
        AsyncMock(return_value="abc"),
    )
    # Decode resolves to an int but no KV row exists → None → 404.
    monkeypatch.setattr(
        "superset.key_value.utils.decode_permalink_id",
        lambda key, salt: 9999,
    )

    class _EmptyDAO:
        def __init__(self, session):
            pass

        async def get_value_by_key(self, resource, key):
            return None

    monkeypatch.setattr("superset.db.daos.key_value.AsyncKeyValueDAO", _EmptyDAO)

    get_fn = ExploreController.get_explore.fn
    with pytest.raises(ObjectNotFoundError):
        await get_fn(
            None,
            request=request,
            chart_dao=AsyncMock(),
            dataset_dao=AsyncMock(),
            kv_dao=AsyncMock(),
            query_dao=AsyncMock(),
            security_manager=_explore_sm(),
            current_user=MagicMock(),
            session=AsyncMock(),
        )


# ---------------------------------------------------------------------------
# Datasource resolution priority regression tests
# (superset_old/views/utils.py:284-285 + commands/explore/get.py:129-131)
# ---------------------------------------------------------------------------


async def test_datasource_priority_form_data_wins_over_url_param():
    """form_data["datasource"] MUST win over ?datasource_id URL param.

    Original: get_datasource_info() reads form_data["datasource"] first;
    the URL datasource_id is only a fallback.  If form_data carries
    datasource="2__query" and ?datasource_id=5, dataset 2 must be loaded.

    The resolved pair is also written back unconditionally so
    form_data["datasource"] is normalised to "<id>__<type>".
    """
    request = MagicMock()
    # URL param says datasource 5, AND a form_data_key that will supply
    # datasource="2__query" — form_data["datasource"] must win.
    request.query_params = {
        "form_data_key": "test-fd-key",
        "datasource_id": "5",
        "datasource_type": "table",
    }

    chart_dao = AsyncMock()
    chart_dao.find_by_id_with_options.return_value = None
    chart_dao.find_by_id.return_value = None

    # The explore_form_data cache slot supplies form_data with
    # datasource="2__query".
    fake_cache = AsyncMock()
    fake_cache.get.return_value = {
        "datasource_id": 2,
        "datasource_type": "query",
        "chart_id": None,
        "form_data": json.dumps({"datasource": "2__query", "viz_type": "table"}),
    }

    captured_filter: list[object] = []
    dataset_dao = AsyncMock()

    async def _find_all(filters=None, **kw):
        if filters:
            captured_filter.extend(filters)
        return []  # no dataset needed; we only care about which id was looked up

    dataset_dao.find_all = _find_all

    import superset.commands.explore_form_data.utils as _fd_utils

    _fd_utils_orig_check = _fd_utils.check_access

    async def _noop_check(**kw):
        pass

    _fd_utils.check_access = _noop_check
    try:
        from unittest.mock import patch as _patch

        with _patch(
            "superset.controllers.explore_form_data._form_data_cache",
            return_value=fake_cache,
        ):
            get_fn = ExploreController.get_explore.fn
            result = await get_fn(
                None,
                request=request,
                chart_dao=chart_dao,
                dataset_dao=dataset_dao,
                kv_dao=AsyncMock(),
                query_dao=AsyncMock(),
                security_manager=_explore_sm(),
                current_user=MagicMock(),
                session=AsyncMock(),
            )
    finally:
        _fd_utils.check_access = _fd_utils_orig_check

    # Dataset 2 (from form_data) must have been queried, not dataset 5 (URL).
    assert len(captured_filter) == 1
    # SQLAlchemy BinaryExpression: SqlaTable.id == 2
    expr = captured_filter[0]
    assert hasattr(expr, "right"), "expected a SQLAlchemy BinaryExpression"
    assert int(expr.right.value) == 2, (
        f"expected ds_id=2 (form_data wins), got {expr.right.value}"
    )

    # form_data["datasource"] must be normalised to "2__query" (write-back).
    form_data = result["result"]["form_data"]
    assert form_data["datasource"] == "2__query"


async def test_datasource_url_param_fallback_when_no_form_data_datasource():
    """URL datasource_id param is used when form_data has no "datasource" key.

    This is the normal first-visit flow: ?datasource_id=7 with no
    permalink/form_data_key.  The resolved ds_id must be written back into
    form_data["datasource"]="7__table".
    """
    request = MagicMock()
    request.query_params = {"datasource_id": "7", "datasource_type": "table"}

    chart_dao = AsyncMock()
    chart_dao.find_by_id_with_options.return_value = None
    chart_dao.find_by_id.return_value = None

    captured_filter: list[object] = []

    async def _find_all(filters=None, **kw):
        if filters:
            captured_filter.extend(filters)
        return []

    dataset_dao = AsyncMock()
    dataset_dao.find_all = _find_all

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=AsyncMock(),
        query_dao=AsyncMock(),
        security_manager=_explore_sm(),
        current_user=MagicMock(),
        session=AsyncMock(),
    )

    assert len(captured_filter) == 1
    expr = captured_filter[0]
    assert hasattr(expr, "right"), "expected a SQLAlchemy BinaryExpression"
    assert int(expr.right.value) == 7, (
        f"expected ds_id=7 (URL param), got {expr.right.value}"
    )

    # Write-back must normalise form_data["datasource"] = "7__table".
    form_data = result["result"]["form_data"]
    assert form_data["datasource"] == "7__table"


async def test_datasource_writeback_normalises_form_data():
    """After resolution, form_data["datasource"] must always equal
    "<resolved_id>__<resolved_type>" regardless of original source.

    1:1 with superset_old/commands/explore/get.py:129-131 which writes
    form_data["datasource"] unconditionally after get_datasource_info().
    """
    request = MagicMock()
    # form_data_key supplies datasource="3__query"; URL param has a different id.
    request.query_params = {
        "form_data_key": "fd-key-3",
        "datasource_id": "99",
        "datasource_type": "table",
    }

    # The explore_form_data cache slot supplies form_data with
    # datasource="3__query" (the form_data_key branch reads through
    # ``cache_manager.explore_form_data_cache`` — same slot the
    # explore_form_data endpoints write to).
    fake_cache = AsyncMock()
    fake_cache.get.return_value = {
        "datasource_id": 3,
        "datasource_type": "query",
        "chart_id": None,
        "form_data": json.dumps({"datasource": "3__query", "viz_type": "bar"}),
    }

    chart_dao = AsyncMock()
    chart_dao.find_by_id_with_options.return_value = None
    chart_dao.find_by_id.return_value = None

    dataset_dao = AsyncMock()
    dataset_dao.find_all.return_value = []

    import superset.commands.explore_form_data.utils as _fd_utils

    orig_check = _fd_utils.check_access

    async def _noop_check(**kw):
        pass

    _fd_utils.check_access = _noop_check
    try:
        with patch(
            "superset.controllers.explore_form_data._form_data_cache",
            return_value=fake_cache,
        ):
            result = await ExploreController.get_explore.fn(
                None,
                request=request,
                chart_dao=chart_dao,
                dataset_dao=dataset_dao,
                kv_dao=AsyncMock(),
                query_dao=AsyncMock(),
                security_manager=_explore_sm(),
                current_user=MagicMock(),
                session=AsyncMock(),
            )
    finally:
        _fd_utils.check_access = orig_check
    fake_cache.get.assert_awaited_once_with("fd-key-3")

    # form_data["datasource"] must reflect the RESOLVED datasource (3__query),
    # not the stale URL param value (99__table).
    form_data = result["result"]["form_data"]
    assert form_data["datasource"] == "3__query"


# ---------------------------------------------------------------------------
# REJECTED_FORM_DATA_KEYS filter ordering
# (superset_old/views/utils.py:222 — filter BEFORE slice merge)
# ---------------------------------------------------------------------------


async def test_rejected_form_data_keys_slice_stored_js_key_survives(monkeypatch):
    """Chart stored with js_tooltip must keep it even when ENABLE_JAVASCRIPT_CONTROLS
    is disabled.

    Original superset_old/views/utils.py:222 applies the REJECTED_FORM_DATA_KEYS
    filter BEFORE the slice_form_data.update(form_data) merge at line 239.
    The filter only strips JS keys from the *request-submitted* form_data.
    The slice's own stored form_data is NOT filtered, so a chart saved while
    the flag was enabled continues to render those keys after the flag is off.

    Regression: the old liteset code applied the filter AFTER the full merge,
    stripping even the slice's stored JS keys.
    """
    from superset.utils.feature_flags import feature_flag_manager

    # Disable JS controls for this test.
    monkeypatch.setattr(
        feature_flag_manager,
        "is_feature_enabled",
        lambda feat: feat != "ENABLE_JAVASCRIPT_CONTROLS",
    )

    request = MagicMock()
    request.query_params = {"slice_id": "5"}

    chart = MagicMock()
    chart.id = 5
    chart.slice_name = "JS Chart"
    chart.viz_type = "table"
    chart.owners = []
    chart.dashboards = []
    chart.created_by = None
    chart.changed_by = None
    chart.changed_on = None
    chart.created_on = None
    chart.datasource_id = None
    # The chart was saved WITH js_tooltip while the feature was enabled.
    chart.form_data = {"viz_type": "table", "js_tooltip": "my_tooltip_fn"}

    chart_dao = AsyncMock()
    chart_dao.find_by_id_with_options.return_value = chart
    chart_dao.find_by_id.return_value = chart

    result = await ExploreController.get_explore.fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=AsyncMock(),
        kv_dao=AsyncMock(),
        query_dao=AsyncMock(),
        security_manager=_explore_sm(),
        current_user=MagicMock(),
        session=AsyncMock(),
    )

    form_data = result["result"]["form_data"]
    # The slice's OWN stored js_tooltip must survive — filter must not strip it.
    assert "js_tooltip" in form_data, (
        "js_tooltip from chart.form_data must survive the REJECTED_FORM_DATA_KEYS "
        "filter (filter only strips request-submitted data, not stored slice keys)"
    )
    assert form_data["js_tooltip"] == "my_tooltip_fn"


async def test_rejected_form_data_keys_request_js_key_stripped(monkeypatch):
    """js_tooltip arriving via the request (form_data_key / URL arg) is stripped
    when ENABLE_JAVASCRIPT_CONTROLS is disabled.

    This is the security-enforcement side of the same filter:
    superset_old/views/utils.py:222 removes REJECTED_FORM_DATA_KEYS from the
    initial form_data (permalink/form_data_key/URL-arg) before the slice merge.
    A chart without a stored js_tooltip must not receive one from the request.
    """
    from superset.utils.feature_flags import feature_flag_manager

    monkeypatch.setattr(
        feature_flag_manager,
        "is_feature_enabled",
        lambda feat: feat != "ENABLE_JAVASCRIPT_CONTROLS",
    )

    request = MagicMock()
    request.query_params = {"slice_id": "7", "form_data_key": "fdk"}

    chart = MagicMock()
    chart.id = 7
    chart.slice_name = "Safe Chart"
    chart.viz_type = "table"
    chart.owners = []
    chart.dashboards = []
    chart.created_by = None
    chart.changed_by = None
    chart.changed_on = None
    chart.created_on = None
    chart.datasource_id = None
    # Stored chart does NOT have js_tooltip.
    chart.form_data = {"viz_type": "table"}

    chart_dao = AsyncMock()
    chart_dao.find_by_id_with_options.return_value = chart
    chart_dao.find_by_id.return_value = chart

    # The form_data_key cache entry injects js_tooltip via the request path.
    fake_cache = AsyncMock()
    fake_cache.get.return_value = {
        "datasource_id": 0,
        "datasource_type": "table",
        "chart_id": None,
        "form_data": json.dumps({"js_tooltip": "evil_fn", "extra": "ok"}),
    }
    monkeypatch.setattr(
        "superset.controllers.explore_form_data._form_data_cache",
        lambda: fake_cache,
    )

    import superset.commands.explore_form_data.utils as _fd_utils

    orig_check = _fd_utils.check_access

    async def _noop(**kw):
        pass

    _fd_utils.check_access = _noop
    try:
        result = await ExploreController.get_explore.fn(
            None,
            request=request,
            chart_dao=chart_dao,
            dataset_dao=AsyncMock(),
            kv_dao=AsyncMock(),
            query_dao=AsyncMock(),
            security_manager=_explore_sm(),
            current_user=MagicMock(),
            session=AsyncMock(),
        )
    finally:
        _fd_utils.check_access = orig_check

    form_data = result["result"]["form_data"]
    # js_tooltip came from the request (form_data_key), not the stored chart —
    # it MUST be stripped.
    assert "js_tooltip" not in form_data, (
        "js_tooltip from request form_data must be stripped by "
        "REJECTED_FORM_DATA_KEYS filter"
    )
    # Other keys from the request must survive.
    assert form_data.get("extra") == "ok"


# ---------------------------------------------------------------------------
# raise_for_access called unconditionally
# (superset_old/commands/explore/get.py:123 — no hasattr guard)
# ---------------------------------------------------------------------------


async def test_raise_for_access_called_unconditionally():
    """security_manager.raise_for_access must be called when a dataset is found.

    Original superset_old/commands/explore/get.py:123:
        ``security_manager.raise_for_access(datasource=datasource)``
    No hasattr() guard — the call is always made.

    Regression: the old liteset code wrapped it in
    ``if hasattr(security_manager, "raise_for_access"):`` which silently
    skipped the check when the method was absent, letting any authenticated
    ``can_read Explore`` user read any dataset.
    """
    request = MagicMock()
    request.query_params = {"datasource_id": "42", "datasource_type": "table"}

    # A minimal mock dataset that lets dataset_data build without errors.
    mock_dataset = MagicMock()
    mock_dataset.id = 42
    mock_dataset.table_name = "test_table"
    mock_dataset.name = "test_table"
    mock_dataset.default_endpoint = None
    mock_dataset.owners = []
    mock_dataset.columns = []
    mock_dataset.metrics = []
    mock_dataset.database = None

    dataset_dao = AsyncMock()
    dataset_dao.find_all.return_value = [mock_dataset]

    chart_dao = AsyncMock()
    chart_dao.find_by_id_with_options.return_value = None
    chart_dao.find_by_id.return_value = None

    sm = _explore_sm()  # has raise_for_access = AsyncMock()

    await ExploreController.get_explore.fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=AsyncMock(),
        query_dao=AsyncMock(),
        security_manager=sm,
        current_user=MagicMock(),
        session=AsyncMock(),
    )

    # raise_for_access MUST have been called — no hasattr guard should suppress it.
    sm.raise_for_access.assert_called_once()
    call_kwargs = sm.raise_for_access.call_args.kwargs
    assert call_kwargs.get("datasource") is mock_dataset, (
        "raise_for_access must be called with datasource=<the loaded dataset>"
    )
