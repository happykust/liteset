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
"""Ported from ``tests/unit_tests/commands/chart/warm_up_cache_test.py``.

The Liteset port is :class:`superset.commands.chart.warm_up_cache
.WarmUpChartCacheCommand` — an async command. It mirrors the upstream
branching:

* legacy charts (``form_data["viz_type"] in get_active_viz_types()``) go
  through ``superset.viz.get_viz`` -> ``BaseViz.get_payload`` (the upstream
  ``_warm_up_legacy_cache`` path);
* modern charts build query objects from ``chart.query_context`` and run them
  through ``AsyncQueryContextProcessor`` (the upstream non-legacy path that
  resolved to ``ChartDataCommand`` under the hood).

The upstream tests patched module-level Flask seams (``get_form_data``,
``ChartDataCommand``, ``get_dashboard_extra_filters``, ``g``, ``viz_types``,
``get_viz``, ``db``). Those names don't exist in the async port, so the call
sites are adapted to the port's seams while preserving the verified behaviour:
dashboard filters get appended to every query, ``extra_filters`` takes
precedence over dashboard metadata, the canonical
``{chart_id, viz_error, viz_status}`` payload shape, and the error paths.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from superset.commands.chart.warm_up_cache import WarmUpChartCacheCommand
from superset.models.slice import Slice


def _make_command(chart, dashboard_id=None, extra_filters=None):
    """Build the command with an already-loaded chart (validate is a no-op).

    Mirrors upstream ``ChartWarmUpCacheCommand(chart, dashboard_id,
    extra_filters)`` where the chart is passed directly. The async port takes
    a DAO first; a bare ``Mock`` suffices because every code path under test
    is patched at a higher seam and never touches the session.
    """
    return WarmUpChartCacheCommand(
        dao=Mock(),
        dashboard_id=dashboard_id,
        extra_filters=extra_filters,
        chart=chart,
    )


def _modern_chart(**kwargs):
    """A modern (non-legacy) chart whose ``form_data`` viz_type is modern."""
    defaults = {
        "id": 123,
        "slice_name": "Test Chart",
        "viz_type": "echarts_timeseries_bar",
        "datasource_id": 1,
        "datasource_type": "table",
    }
    defaults.update(kwargs)
    return Slice(**defaults)


def _patch_table():
    """Make ``chart.table`` (the datasource) resolve on a transient Slice.

    ``_warm_up_non_legacy_cache`` reads ``chart.table`` directly (the upstream
    ``ChartDataCommand`` resolved the datasource the same way). The relationship
    is a class attribute, so override it with a property for the test duration.
    """
    mock_datasource = Mock()
    mock_datasource.id = 1
    mock_datasource.type = "table"
    return patch.object(Slice, "table", property(lambda self: mock_datasource))


def _patch_active_viz_types(types):
    """Patch the legacy-viz registry consulted by ``run``.

    ``run`` imports ``get_active_viz_types`` from ``superset.viz`` at call
    time, so patching it there is honoured. The return value only needs
    ``__contains__`` to behave like the upstream ``viz_types`` list.
    """
    return patch("superset.viz.get_active_viz_types", return_value=types)


async def test_applies_dashboard_filters_to_non_legacy_chart():
    """Verify dashboard filters are added to every query for non-legacy viz."""
    chart = _modern_chart(id=123)

    mock_query = Mock()
    mock_query.filters = []
    mock_qc = MagicMock()
    mock_qc.queries = [mock_query]

    dashboard_filters = [{"col": "country", "op": "in", "val": ["USA", "France"]}]

    command = _make_command(chart, dashboard_id=42)

    with (
        _patch_active_viz_types({}),
        _patch_table(),
        patch.object(command, "_build_queries", return_value=mock_qc.queries),
        patch.object(
            command,
            "_get_dashboard_filters",
            new=AsyncMock(return_value=dashboard_filters),
        ) as mock_get_filters,
        patch(
            "superset.common.query_context_processor.AsyncQueryContextProcessor"
        ) as mock_processor,
        patch.object(chart, "query_context", '{"queries": [{}]}', create=True),
    ):
        mock_processor.return_value.get_payload = AsyncMock(
            return_value={"queries": [{"error": None, "status": "success"}]}
        )
        result = await command.run()

    # Filters were appended to the query.
    assert mock_query.filters == [
        {"col": "country", "op": "in", "val": ["USA", "France"]}
    ]
    # Dashboard filters were resolved for this chart.
    mock_get_filters.assert_awaited_once_with(123)
    # force should be set to True: the warm-up forces a cache refresh on the
    # query context. Upstream asserted ``mock_qc.force is True``; the port
    # builds a real ``AsyncQueryContext(force=True)`` and forwards ``force=True``
    # to the mocked processor's ``get_payload`` (the verifiable seam here).
    assert mock_processor.return_value.get_payload.await_args.kwargs["force"] is True
    assert result["chart_id"] == 123
    assert result["viz_error"] is None


async def test_no_filters_applied_without_dashboard_id():
    """Verify no filters are added when dashboard_id is not provided."""
    chart = _modern_chart(id=124, viz_type="big_number")

    mock_query = Mock()
    mock_query.filters = [{"col": "existing", "op": "==", "val": "filter"}]
    mock_qc = MagicMock()
    mock_qc.queries = [mock_query]

    command = _make_command(chart, dashboard_id=None)

    with (
        _patch_active_viz_types({}),
        _patch_table(),
        patch.object(command, "_build_queries", return_value=mock_qc.queries),
        patch(
            "superset.common.query_context_processor.AsyncQueryContextProcessor"
        ) as mock_processor,
        patch.object(chart, "query_context", '{"queries": [{}]}', create=True),
    ):
        mock_processor.return_value.get_payload = AsyncMock(
            return_value={"queries": [{"error": None, "status": "success"}]}
        )
        await command.run()

    # Filter list unchanged (no dashboard filters added).
    assert mock_query.filters == [{"col": "existing", "op": "==", "val": "filter"}]


async def test_extra_filters_parameter_takes_precedence():
    """Verify extra_filters is used instead of fetching from dashboard."""
    chart = _modern_chart(id=125, viz_type="pie")

    mock_query = Mock()
    mock_query.filters = []
    mock_qc = MagicMock()
    mock_qc.queries = [mock_query]

    extra_filters_json = '[{"col": "state", "op": "==", "val": "CA"}]'
    command = _make_command(chart, dashboard_id=42, extra_filters=extra_filters_json)

    # Spy on the dashboard-metadata fetch to ensure it is NOT consulted.
    with (
        _patch_active_viz_types({}),
        _patch_table(),
        patch.object(command, "_build_queries", return_value=mock_qc.queries),
        patch.object(
            command,
            "_build_dashboard_extra_filters",
            new=AsyncMock(return_value=[]),
        ) as mock_build_dashboard,
        patch(
            "superset.common.query_context_processor.AsyncQueryContextProcessor"
        ) as mock_processor,
        patch.object(chart, "query_context", '{"queries": [{}]}', create=True),
    ):
        mock_processor.return_value.get_payload = AsyncMock(
            return_value={"queries": [{"error": None, "status": "success"}]}
        )
        await command.run()

    # The dashboard-metadata path is skipped when extra_filters is provided.
    mock_build_dashboard.assert_not_awaited()
    # extra_filters were parsed and applied.
    assert mock_query.filters == [{"col": "state", "op": "==", "val": "CA"}]


async def test_handles_multiple_queries_in_query_context():
    """Verify filters are added to ALL queries in the query context."""
    chart = _modern_chart(id=126, viz_type="heatmap_v2")

    mock_query1 = Mock()
    mock_query1.filters = []
    mock_query2 = Mock()
    mock_query2.filters = []
    mock_qc = MagicMock()
    mock_qc.queries = [mock_query1, mock_query2]

    dashboard_filters = [{"col": "country", "op": "==", "val": "USA"}]
    command = _make_command(chart, dashboard_id=42)

    with (
        _patch_active_viz_types({}),
        _patch_table(),
        patch.object(command, "_build_queries", return_value=mock_qc.queries),
        patch.object(
            command,
            "_get_dashboard_filters",
            new=AsyncMock(return_value=dashboard_filters),
        ),
        patch(
            "superset.common.query_context_processor.AsyncQueryContextProcessor"
        ) as mock_processor,
        patch.object(chart, "query_context", '{"queries": [{}, {}]}', create=True),
    ):
        mock_processor.return_value.get_payload = AsyncMock(
            return_value={
                "queries": [
                    {"error": None, "status": "success"},
                    {"error": None, "status": "success"},
                ]
            }
        )
        await command.run()

    assert len(mock_query1.filters) == 1
    assert len(mock_query2.filters) == 1
    assert mock_query1.filters[0]["col"] == "country"
    assert mock_query2.filters[0]["col"] == "country"


async def test_handles_empty_dashboard_filters():
    """Verify graceful handling when dashboard has no filters configured."""
    chart = _modern_chart(id=127, viz_type="echarts_area")

    mock_query = Mock()
    mock_query.filters = []
    mock_qc = MagicMock()
    mock_qc.queries = [mock_query]

    command = _make_command(chart, dashboard_id=42)

    with (
        _patch_active_viz_types({}),
        _patch_table(),
        patch.object(command, "_build_queries", return_value=mock_qc.queries),
        patch.object(
            command, "_get_dashboard_filters", new=AsyncMock(return_value=[])
        ) as mock_get_filters,
        patch(
            "superset.common.query_context_processor.AsyncQueryContextProcessor"
        ) as mock_processor,
        patch.object(chart, "query_context", '{"queries": [{}]}', create=True),
    ):
        mock_processor.return_value.get_payload = AsyncMock(
            return_value={"queries": [{"error": None, "status": "success"}]}
        )
        await command.run()

    # No filters added (empty list case), but the resolver was still called.
    assert mock_query.filters == []
    mock_get_filters.assert_awaited()


async def test_invalid_json_in_extra_filters_raises_error():
    """Verify that invalid JSON in extra_filters surfaces as a viz_error."""
    chart = _modern_chart(id=128, viz_type="pie")

    mock_query = Mock()
    mock_query.filters = []
    mock_qc = MagicMock()
    mock_qc.queries = [mock_query]

    # Invalid JSON string - missing closing brace.
    invalid_json = '{"col": "state", "op": "==", "val": ["CA"]'
    command = _make_command(chart, dashboard_id=42, extra_filters=invalid_json)

    with (
        _patch_active_viz_types({}),
        _patch_table(),
        patch.object(command, "_build_queries", return_value=mock_qc.queries),
        patch.object(chart, "query_context", '{"queries": [{}]}', create=True),
    ):
        result = await command.run()

    assert result["viz_error"] is not None
    assert result["chart_id"] == 128
    error_str = str(result["viz_error"]).lower()
    assert (
        "json" in error_str
        or "decode" in error_str
        or "expecting" in error_str
        or "delimiter" in error_str
    ), f"Error should be a JSON decode issue: {result['viz_error']}"


async def test_none_query_context_raises_chart_invalid_error():
    """Verify that an absent query context surfaces as a viz_error."""
    chart = _modern_chart(id=129, viz_type="echarts_timeseries")
    command = _make_command(chart, dashboard_id=None)

    with (
        _patch_active_viz_types({}),
        patch.object(chart, "query_context", None, create=True),
    ):
        result = await command.run()

    assert result["viz_error"] is not None
    assert result["chart_id"] == 129
    error_str = str(result["viz_error"]).lower()
    assert "query context" in error_str, (
        f"Error should mention query context: {result['viz_error']}"
    )
    assert "not exist" in error_str, (
        f"Error should mention not exist: {result['viz_error']}"
    )


async def test_legacy_chart_without_datasource_raises_error():
    """Verify that a legacy chart without datasource surfaces a viz_error."""
    chart = Slice(
        id=130,
        slice_name="Legacy Chart",
        viz_type="table",
        datasource_id=None,
        datasource_type=None,
    )
    command = _make_command(chart, dashboard_id=None)

    with (
        _patch_active_viz_types({"table": object}),
        patch.object(
            type(chart),
            "datasource",
            new_callable=lambda: property(lambda self: None),
        ),
    ):
        result = await command.run()

    assert result["viz_error"] is not None
    assert result["chart_id"] == 130
    error_str = str(result["viz_error"]).lower()
    assert "datasource" in error_str, (
        f"Error should mention datasource: {result['viz_error']}"
    )
    assert "not exist" in error_str, (
        f"Error should mention not exist: {result['viz_error']}"
    )


async def test_legacy_chart_warm_up_with_dashboard():
    """Test successful legacy chart warm-up with dashboard filters."""
    chart = Slice(
        id=131,
        slice_name="Legacy Table",
        viz_type="table",
        datasource_id=1,
        datasource_type="table",
    )

    mock_datasource = Mock()
    mock_datasource.type = "table"
    mock_datasource.id = 1

    mock_viz = Mock()
    mock_viz.get_payload = AsyncMock(return_value={"errors": None, "status": "success"})

    dashboard_filters = [{"col": "country", "op": "==", "val": "USA"}]
    command = _make_command(chart, dashboard_id=42)

    with (
        _patch_active_viz_types({"table": object}),
        patch("superset.viz.get_viz", return_value=mock_viz),
        patch.object(
            type(chart),
            "datasource",
            new_callable=lambda: property(lambda self: mock_datasource),
        ),
        patch.object(
            command,
            "_get_dashboard_filters",
            new=AsyncMock(return_value=dashboard_filters),
        ) as mock_get_filters,
    ):
        result = await command.run()

    assert result["chart_id"] == 131
    assert result["viz_error"] is None
    assert result["viz_status"] == "success"
    mock_get_filters.assert_awaited_once_with(131)


async def test_legacy_chart_warm_up_without_dashboard():
    """Test successful legacy chart warm-up without dashboard."""
    chart = Slice(
        id=134,
        slice_name="Legacy Table",
        viz_type="table",
        datasource_id=1,
        datasource_type="table",
    )

    mock_datasource = Mock()
    mock_datasource.type = "table"
    mock_datasource.id = 1

    mock_viz = Mock()
    mock_viz.get_payload = AsyncMock(return_value={"errors": None, "status": "success"})

    command = _make_command(chart, dashboard_id=None)

    with (
        _patch_active_viz_types({"table": object}),
        patch("superset.viz.get_viz", return_value=mock_viz),
        patch.object(
            type(chart),
            "datasource",
            new_callable=lambda: property(lambda self: mock_datasource),
        ),
    ):
        result = await command.run()

    assert result["chart_id"] == 134
    assert result["viz_error"] is None
    assert result["viz_status"] == "success"


async def test_non_legacy_chart_returns_first_error():
    """Test that the first query error is returned when multiple exist."""
    chart = _modern_chart(id=132, viz_type="echarts_timeseries")

    mock_query = Mock()
    mock_query.filters = []
    mock_qc = MagicMock()
    mock_qc.queries = [mock_query]

    command = _make_command(chart, dashboard_id=None)

    with (
        _patch_active_viz_types({}),
        _patch_table(),
        patch.object(command, "_build_queries", return_value=mock_qc.queries),
        patch(
            "superset.common.query_context_processor.AsyncQueryContextProcessor"
        ) as mock_processor,
        patch.object(chart, "query_context", '{"queries": [{}]}', create=True),
    ):
        mock_processor.return_value.get_payload = AsyncMock(
            return_value={
                "queries": [
                    {"error": "Database connection failed", "status": "failed"},
                    {"error": None, "status": "success"},
                ]
            }
        )
        result = await command.run()

    assert result["chart_id"] == 132
    assert result["viz_error"] == "Database connection failed"
    assert result["viz_status"] == "failed"


async def test_validate_with_integer_chart_id():
    """Validation resolves an integer chart id from the DB.

    1:1 with upstream ``test_validate_with_integer_chart_id``: passing an
    integer chart id (133) makes ``validate`` query the session for the
    matching ``Slice`` and stash it. The upstream assertions
    (``command._chart_or_id == chart`` and the session query was issued
    once) map to ``command._chart is chart`` and a single
    ``session.execute`` round-trip in the async port.
    """
    chart = Slice(id=133, slice_name="Test Chart")
    dao = Mock()
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.one_or_none.return_value = chart
    session.execute = AsyncMock(return_value=exec_result)
    dao.session = session

    command = WarmUpChartCacheCommand(dao=dao, chart_id=133)
    await command.validate()

    assert command._chart is chart
    session.execute.assert_called_once()


async def test_validate_with_loaded_chart_is_noop():
    """A command built with a loaded Slice does not hit the session.

    Mirrors upstream ``validate`` resolving an integer chart-id from the DB;
    in the port, ``validate`` returns immediately when a chart was supplied
    (the caller eager-loaded it), so no query is issued.
    """
    chart = Slice(id=133, slice_name="Test Chart")
    dao = Mock()
    dao.session = MagicMock()
    command = WarmUpChartCacheCommand(dao=dao, chart=chart)

    await command.validate()

    assert command._chart is chart
    dao.session.execute.assert_not_called()


async def test_validate_with_nonexistent_chart_id():
    """Validation raises when the chart id does not exist."""
    from superset.exceptions import ObjectNotFoundError

    dao = Mock()
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.one_or_none.return_value = None
    session.execute = AsyncMock(return_value=exec_result)
    dao.session = session

    command = WarmUpChartCacheCommand(dao=dao, chart_id=99999)

    with pytest.raises(ObjectNotFoundError):
        await command.validate()
