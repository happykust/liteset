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
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.controllers.explore import ExploreController
from superset.exceptions import ObjectNotFoundError


def _explore_sm() -> MagicMock:
    """Permissive security manager; denial scenarios are covered by
    test_controller_rbac_access.
    """
    sm = MagicMock()
    sm.raise_for_access = AsyncMock()
    return sm


def test_controller_path():
    assert ExploreController.path == "/api/v1/explore"


def test_controller_tags():
    assert ExploreController.tags == ["Explore"]


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
    chart.owners = []
    chart.dashboards = []
    chart.created_by = None
    chart.changed_by = None
    chart.changed_on = None
    chart.created_on = None
    chart.datasource_id = None

    chart_dao = AsyncMock()
    chart_dao.find_by_id_with_options.return_value = chart
    # Also stub find_by_id so datasource_id=None propagates and no dataset lookup
    # is triggered (a fresh AsyncMock return value has a truthy default_endpoint
    # which triggers a spurious 302 redirect).
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
    assert result["result"]["message"] is None


async def test_get_explore_with_permalink_key_round_trip(monkeypatch):
    """A permalink_key (hashids string) must be decoded → int id, looked up in
    the EXPLORE_PERMALINK resource, and its ``state.formData`` returned.

    Consistent with how CreateExplorePermalinkCommand stores the payload:
    ``{..., "state": {"formData": {...}, "urlParams": [...]}}`` keyed by an
    auto-generated integer id, encoded into the URL via ``encode_permalink_key``.
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

    assert captured["resource"] == "explore_permalink"
    assert captured["key"] == 42
    form_data = result["result"]["form_data"]
    assert form_data["viz_type"] == "bar"
    assert form_data["url_params"]["foo"] == "bar"


async def test_get_explore_with_bad_permalink_key_404(monkeypatch):
    """Decodable key with no matching KV row → 404."""
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
        return []

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

    assert len(captured_filter) == 1
    # SQLAlchemy BinaryExpression: SqlaTable.id == 2
    expr = captured_filter[0]
    assert hasattr(expr, "right"), "expected a SQLAlchemy BinaryExpression"
    assert int(expr.right.value) == 2, (
        f"expected ds_id=2 (form_data wins), got {expr.right.value}"
    )
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
    form_data = result["result"]["form_data"]
    assert form_data["datasource"] == "7__table"


async def test_datasource_writeback_normalises_form_data():
    """After resolution, form_data["datasource"] must always equal
    "<resolved_id>__<resolved_type>" regardless of original source.
    """
    request = MagicMock()
    # form_data_key supplies datasource="3__query"; URL param has a different id.
    request.query_params = {
        "form_data_key": "fd-key-3",
        "datasource_id": "99",
        "datasource_type": "table",
    }

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
    form_data = result["result"]["form_data"]
    assert form_data["datasource"] == "3__query"


async def test_rejected_form_data_keys_slice_stored_js_key_survives(monkeypatch):
    """Chart stored with js_tooltip must keep it even when ENABLE_JAVASCRIPT_CONTROLS
    is disabled.

    The REJECTED_FORM_DATA_KEYS filter is applied BEFORE the
    slice_form_data.update(form_data) merge.
    The filter only strips JS keys from the *request-submitted* form_data.
    The slice's own stored form_data is NOT filtered, so a chart saved while
    the flag was enabled continues to render those keys after the flag is off.

    Regression: the old liteset code applied the filter AFTER the full merge,
    stripping even the slice's stored JS keys.
    """
    from superset.utils.feature_flags import feature_flag_manager

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
    assert "js_tooltip" in form_data, (
        "js_tooltip from chart.form_data must survive the REJECTED_FORM_DATA_KEYS "
        "filter (filter only strips request-submitted data, not stored slice keys)"
    )
    assert form_data["js_tooltip"] == "my_tooltip_fn"


async def test_rejected_form_data_keys_request_js_key_stripped(monkeypatch):
    """js_tooltip arriving via the request (form_data_key / URL arg) is stripped
    when ENABLE_JAVASCRIPT_CONTROLS is disabled.

    This is the security-enforcement side of the same filter:
    REJECTED_FORM_DATA_KEYS are removed from the initial form_data
    (permalink/form_data_key/URL-arg) before the slice merge.
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
    chart.form_data = {"viz_type": "table"}

    chart_dao = AsyncMock()
    chart_dao.find_by_id_with_options.return_value = chart
    chart_dao.find_by_id.return_value = chart

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
    assert "js_tooltip" not in form_data, (
        "js_tooltip from request form_data must be stripped by "
        "REJECTED_FORM_DATA_KEYS filter"
    )
    assert form_data.get("extra") == "ok"


async def test_raise_for_access_called_unconditionally():
    """security_manager.raise_for_access must be called when a dataset is found;
    the call is unconditional — no hasattr() guard.

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

    sm = _explore_sm()

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

    sm.raise_for_access.assert_called_once()
    call_kwargs = sm.raise_for_access.call_args.kwargs
    assert call_kwargs.get("datasource") is mock_dataset, (
        "raise_for_access must be called with datasource=<the loaded dataset>"
    )
