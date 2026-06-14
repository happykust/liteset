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
"""Flask-free port of ``tests/integration_tests/viz_tests.py``.

Exercises the legacy ``superset.viz`` BaseViz / NVD3 / Partition / DeckGL
visualization classes against the real (async) Liteset port. Most cases are
pure dataframe transforms that need no database; the DeckGL multi-layer cases
drive the async sub-layer loader against the seeded Postgres backend via the
``db_session`` fixture and factory-created slices.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

import superset.viz as viz
from superset.config import SupersetSettings
from superset.exceptions import QueryObjectValidationError, SpatialException
from superset.models.connectors import SqlaTable
from superset.models.slice import Slice
from superset.viz import DTTM_ALIAS
from tests.superset.integration import factories as f

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers (replacing SupersetTestCase.get_datasource_mock and the JSON
# fixture loader, which both live in the Flask-coupled integration package).
# ---------------------------------------------------------------------------


def get_datasource_mock() -> Any:
    """Mirror ``SupersetTestCase.get_datasource_mock`` without Flask imports."""
    datasource = MagicMock()
    results = Mock()
    results.query = Mock()
    results.status = Mock()
    results.error_message = None
    results.df = pd.DataFrame()
    datasource.type = "table"
    datasource.query = Mock(return_value=results)
    mock_dttm_col = Mock()
    datasource.get_col = Mock(return_value=mock_dttm_col)
    datasource.query = Mock(return_value=results)
    datasource.database = Mock()
    datasource.database.db_engine_spec = Mock()
    datasource.database.perm = "mock_database_perm"
    datasource.schema_perm = "mock_schema_perm"
    datasource.perm = "mock_datasource_perm"
    datasource.__class__ = SqlaTable
    datasource.database.db_engine_spec.mutate_expression_label = lambda x: x
    datasource.owners = MagicMock()
    datasource.id = 99999
    return datasource


# The deck.gl form-data fixtures (inlined 1:1 from the upstream
# tests/integration_tests/fixtures/deck_*_form_data.json files).
DECK_PATH_FORM_DATA: dict[str, Any] = {
    "color_picker": {"a": 1, "b": 135, "g": 122, "r": 0},
    "datasource": "12__table",
    "filters": [],
    "having": "",
    "js_columns": ["color"],
    "js_datapoint_mutator": (
        "d => {\n return {\n ...d,\n color: colors.hexToRGB(d.extraProps.color),\n }\n}"
    ),
    "js_onclick_href": "",
    "js_tooltip": "",
    "line_column": "path_json",
    "line_type": "json",
    "line_width": 150,
    "mapbox_style": "mapbox://styles/mapbox/light-v9",
    "reverse_long_lat": False,
    "row_limit": 5000,
    "since": "7 days ago",
    "slice_id": 1013,
    "time_grain_sqla": None,
    "until": "now",
    "viewport": {
        "altitude": 1.5,
        "bearing": 0,
        "height": 1094,
        "latitude": 37.73671752604488,
        "longitude": -122.18885402582598,
        "maxLatitude": 85.05113,
        "maxPitch": 60,
        "maxZoom": 20,
        "minLatitude": -85.05113,
        "minPitch": 0,
        "minZoom": 0,
        "pitch": 0,
        "width": 669,
        "zoom": 9.51847667620428,
    },
    "viz_type": "deck_path",
    "where": "",
    "granularity_sqla": None,
    "autozoom": True,
    "url_params": {},
    "size": "100",
}

DECK_GEOJSON_FORM_DATA: dict[str, Any] = {
    "color_picker": {"a": 1, "b": 135, "g": 122, "r": 0},
    "datasource": "12__table",
    "filters": [],
    "having": "",
    "js_columns": ["color"],
    "js_datapoint_mutator": (
        "d => {\n return {\n ...d,\n color: colors.hexToRGB(d.extraProps.color),\n }\n}"
    ),
    "js_onclick_href": "",
    "js_tooltip": "",
    "mapbox_style": "mapbox://styles/mapbox/light-v9",
    "reverse_long_lat": False,
    "row_limit": 5000,
    "since": "7 days ago",
    "slice_id": 1013,
    "time_grain_sqla": None,
    "until": "now",
    "geojson": "test_col",
    "viewport": {
        "altitude": 1.5,
        "bearing": 0,
        "height": 1094,
        "latitude": 37.73671752604488,
        "longitude": -122.18885402582598,
        "maxLatitude": 85.05113,
        "maxPitch": 60,
        "maxZoom": 20,
        "minLatitude": -85.05113,
        "minPitch": 0,
        "minZoom": 0,
        "pitch": 0,
        "width": 669,
        "zoom": 9.51847667620428,
    },
    "viz_type": "deck_geojson",
    "where": "",
    "granularity_sqla": None,
    "autozoom": True,
    "url_params": {},
    "size": "100",
}


# ---------------------------------------------------------------------------
# TestBaseViz
# ---------------------------------------------------------------------------


class TestBaseViz:
    async def test_constructor_exception_no_datasource(self) -> None:
        form_data: dict[str, Any] = {}
        datasource = None
        with pytest.raises(Exception):  # noqa: B017, PT011
            viz.BaseViz(datasource, form_data)

    async def test_process_metrics(self) -> None:
        # test TimeTableViz metrics in correct order
        form_data = {
            "url_params": {},
            "row_limit": 500,
            "metric": "sum__SP_POP_TOTL",
            "entity": "country_code",
            "secondary_metric": "sum__SP_POP_TOTL",
            "granularity_sqla": "year",
            "page_length": 0,
            "all_columns": [],
            "viz_type": "time_table",
            "since": "2014-01-01",
            "until": "2014-01-02",
            "metrics": ["sum__SP_POP_TOTL", "SUM(SE_PRM_NENR_MA)", "SUM(SP_URB_TOTL)"],
            "country_fieldtype": "cca3",
            "percent_metrics": ["count"],
            "slice_id": 74,
            "time_grain_sqla": None,
            "order_by_cols": [],
            "groupby": ["country_name"],
            "compare_lag": "10",
            "limit": "25",
            "datasource": "2__table",
            "table_timestamp_format": "%Y-%m-%d %H:%M:%S",
            "markup_type": "markdown",
            "where": "",
            "compare_suffix": "o10Y",
        }
        datasource = Mock()
        datasource.type = "table"
        test_viz = viz.BaseViz(datasource, form_data)
        expect_metric_labels = [
            "sum__SP_POP_TOTL",
            "SUM(SE_PRM_NENR_MA)",
            "SUM(SP_URB_TOTL)",
            "count",
        ]
        assert test_viz.metric_labels == expect_metric_labels
        assert test_viz.all_metrics == expect_metric_labels

    async def test_get_df_returns_empty_df(self) -> None:
        form_data = {"dummy": 123}
        query_obj = {"granularity": "day"}
        datasource = get_datasource_mock()
        # The Liteset port resolves data through ``async_query``; an empty df
        # in -> empty df out (matching the upstream sync ``query`` behaviour).
        datasource.async_query = AsyncMock(return_value=datasource.query.return_value)
        test_viz = viz.BaseViz(datasource, form_data)
        result = await test_viz.get_df(query_obj)
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    async def test_get_df_handles_dttm_col(self) -> None:
        form_data = {"dummy": 123}
        query_obj = {"granularity": "day"}
        results = Mock()
        results.query = Mock()
        results.status = Mock()
        results.error_message = Mock()
        results.errors = []
        results.applied_filter_columns = []
        results.rejected_filter_columns = []
        datasource = Mock()
        datasource.type = "table"
        # Liteset's async get_df awaits ``async_query`` and reads the granularity
        # column's ``python_date_format`` through ``get_column``.
        datasource.async_query = AsyncMock(return_value=results)
        mock_dttm_col = Mock()
        datasource.get_column = Mock(return_value=mock_dttm_col)

        test_viz = viz.BaseViz(datasource, form_data)
        test_viz.df_metrics_to_num = Mock()
        test_viz.get_fillna_for_columns = Mock(return_value=0)

        results.df = pd.DataFrame(data={DTTM_ALIAS: ["1960-01-01 05:00:00"]})
        datasource.offset = 0
        mock_dttm_col.python_date_format = "epoch_ms"
        result = await test_viz.get_df(query_obj)
        pd.testing.assert_series_equal(
            result[DTTM_ALIAS], pd.Series([datetime(1960, 1, 1, 5, 0)], name=DTTM_ALIAS)
        )

        results.df = pd.DataFrame(data={DTTM_ALIAS: ["1960-01-01 05:00:00"]})
        mock_dttm_col.python_date_format = None
        result = await test_viz.get_df(query_obj)
        pd.testing.assert_series_equal(
            result[DTTM_ALIAS], pd.Series([datetime(1960, 1, 1, 5, 0)], name=DTTM_ALIAS)
        )

        results.df = pd.DataFrame(data={DTTM_ALIAS: ["1960-01-01 05:00:00"]})
        datasource.offset = 1
        result = await test_viz.get_df(query_obj)
        pd.testing.assert_series_equal(
            result[DTTM_ALIAS], pd.Series([datetime(1960, 1, 1, 6, 0)], name=DTTM_ALIAS)
        )

        datasource.offset = 0
        results.df = pd.DataFrame(data={DTTM_ALIAS: ["1960-01-01"]})
        mock_dttm_col.python_date_format = "%Y-%m-%d"
        result = await test_viz.get_df(query_obj)
        pd.testing.assert_series_equal(
            result[DTTM_ALIAS], pd.Series([datetime(1960, 1, 1, 0, 0)], name=DTTM_ALIAS)
        )

    async def test_cache_timeout(self) -> None:
        # In the Liteset port ``cache_timeout`` resolves the deployment default
        # from ``settings.cache_default_timeout`` rather than the Flask
        # ``current_app.config["DATA_CACHE_CONFIG"]``. The precedence chain
        # (form_data -> datasource -> database -> settings default) is unchanged.
        settings = SupersetSettings()  # type: ignore[call-arg]

        datasource = get_datasource_mock()
        datasource.cache_timeout = 0
        test_viz = viz.BaseViz(datasource, form_data={}, settings=settings)
        assert test_viz.cache_timeout == 0

        datasource.cache_timeout = 156
        test_viz = viz.BaseViz(datasource, form_data={}, settings=settings)
        assert test_viz.cache_timeout == 156

        datasource.cache_timeout = None
        datasource.database.cache_timeout = 0
        test_viz = viz.BaseViz(datasource, form_data={}, settings=settings)
        assert test_viz.cache_timeout == 0

        datasource.database.cache_timeout = 1666
        test_viz = viz.BaseViz(datasource, form_data={}, settings=settings)
        assert test_viz.cache_timeout == 1666

        datasource.database.cache_timeout = None
        test_viz = viz.BaseViz(datasource, form_data={}, settings=settings)
        assert test_viz.cache_timeout == settings.cache_default_timeout

        # With no settings the property falls back to the hardcoded 300 default.
        test_viz = viz.BaseViz(datasource, form_data={})
        assert test_viz.cache_timeout == 300


# ---------------------------------------------------------------------------
# TestPairedTTest
# ---------------------------------------------------------------------------


class TestPairedTTest:
    async def test_get_data_transforms_dataframe(self) -> None:
        form_data = {
            "groupby": ["groupA", "groupB", "groupC"],
            "metrics": ["metric1", "metric2", "metric3"],
        }
        datasource = get_datasource_mock()
        raw: dict[Any, Any] = {}
        raw[DTTM_ALIAS] = [100, 200, 300, 100, 200, 300, 100, 200, 300]
        raw["groupA"] = ["a1", "a1", "a1", "b1", "b1", "b1", "c1", "c1", "c1"]
        raw["groupB"] = ["a2", "a2", "a2", "b2", "b2", "b2", "c2", "c2", "c2"]
        raw["groupC"] = ["a3", "a3", "a3", "b3", "b3", "b3", "c3", "c3", "c3"]
        raw["metric1"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        raw["metric2"] = [10, 20, 30, 40, 50, 60, 70, 80, 90]
        raw["metric3"] = [100, 200, 300, 400, 500, 600, 700, 800, 900]
        df = pd.DataFrame(raw)
        paired_ttest_viz = viz.viz_types["paired_ttest"](datasource, form_data)
        data = paired_ttest_viz.get_data(df)
        expected = {
            "metric1": [
                {
                    "values": [
                        {"x": 100, "y": 1},
                        {"x": 200, "y": 2},
                        {"x": 300, "y": 3},
                    ],
                    "group": ("a1", "a2", "a3"),
                },
                {
                    "values": [
                        {"x": 100, "y": 4},
                        {"x": 200, "y": 5},
                        {"x": 300, "y": 6},
                    ],
                    "group": ("b1", "b2", "b3"),
                },
                {
                    "values": [
                        {"x": 100, "y": 7},
                        {"x": 200, "y": 8},
                        {"x": 300, "y": 9},
                    ],
                    "group": ("c1", "c2", "c3"),
                },
            ],
            "metric2": [
                {
                    "values": [
                        {"x": 100, "y": 10},
                        {"x": 200, "y": 20},
                        {"x": 300, "y": 30},
                    ],
                    "group": ("a1", "a2", "a3"),
                },
                {
                    "values": [
                        {"x": 100, "y": 40},
                        {"x": 200, "y": 50},
                        {"x": 300, "y": 60},
                    ],
                    "group": ("b1", "b2", "b3"),
                },
                {
                    "values": [
                        {"x": 100, "y": 70},
                        {"x": 200, "y": 80},
                        {"x": 300, "y": 90},
                    ],
                    "group": ("c1", "c2", "c3"),
                },
            ],
            "metric3": [
                {
                    "values": [
                        {"x": 100, "y": 100},
                        {"x": 200, "y": 200},
                        {"x": 300, "y": 300},
                    ],
                    "group": ("a1", "a2", "a3"),
                },
                {
                    "values": [
                        {"x": 100, "y": 400},
                        {"x": 200, "y": 500},
                        {"x": 300, "y": 600},
                    ],
                    "group": ("b1", "b2", "b3"),
                },
                {
                    "values": [
                        {"x": 100, "y": 700},
                        {"x": 200, "y": 800},
                        {"x": 300, "y": 900},
                    ],
                    "group": ("c1", "c2", "c3"),
                },
            ],
        }
        assert data == expected

    async def test_get_data_empty_null_keys(self) -> None:
        form_data: dict[str, Any] = {"groupby": [], "metrics": [""]}
        datasource = get_datasource_mock()
        raw: dict[Any, Any] = {}
        raw[DTTM_ALIAS] = [100, 200, 300]
        raw[""] = [1, 2, 3]
        raw[None] = [10, 20, 30]

        df = pd.DataFrame(raw)
        paired_ttest_viz = viz.viz_types["paired_ttest"](datasource, form_data)
        data = paired_ttest_viz.get_data(df)
        expected = {
            "N/A": [
                {
                    "values": [
                        {"x": 100, "y": 1},
                        {"x": 200, "y": 2},
                        {"x": 300, "y": 3},
                    ],
                    "group": "All",
                }
            ],
        }
        assert data == expected

        form_data = {"groupby": [], "metrics": [None]}
        with pytest.raises(ValueError):  # noqa: PT011
            viz.viz_types["paired_ttest"](datasource, form_data)


# ---------------------------------------------------------------------------
# TestPartitionViz
# ---------------------------------------------------------------------------


class TestPartitionViz:
    @patch("superset.viz.BaseViz.query_obj")
    async def test_query_obj_time_series_option(self, super_query_obj: Mock) -> None:
        datasource = get_datasource_mock()
        form_data: dict[str, Any] = {}
        test_viz = viz.PartitionViz(datasource, form_data)
        super_query_obj.return_value = {}
        query_obj = test_viz.query_obj()
        assert not query_obj["is_timeseries"]
        test_viz.form_data["time_series_option"] = "agg_sum"
        query_obj = test_viz.query_obj()
        assert query_obj["is_timeseries"]

    async def test_levels_for_computes_levels(self) -> None:
        raw: dict[Any, Any] = {}
        raw[DTTM_ALIAS] = [100, 200, 300, 100, 200, 300, 100, 200, 300]
        raw["groupA"] = ["a1", "a1", "a1", "b1", "b1", "b1", "c1", "c1", "c1"]
        raw["groupB"] = ["a2", "a2", "a2", "b2", "b2", "b2", "c2", "c2", "c2"]
        raw["groupC"] = ["a3", "a3", "a3", "b3", "b3", "b3", "c3", "c3", "c3"]
        raw["metric1"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        raw["metric2"] = [10, 20, 30, 40, 50, 60, 70, 80, 90]
        raw["metric3"] = [100, 200, 300, 400, 500, 600, 700, 800, 900]
        df = pd.DataFrame(raw)
        groups = ["groupA", "groupB", "groupC"]
        time_op = "agg_sum"
        test_viz = viz.PartitionViz(Mock(), {})
        levels = test_viz.levels_for(time_op, groups, df)
        assert len(levels) == 4
        expected = {DTTM_ALIAS: 1800, "metric1": 45, "metric2": 450, "metric3": 4500}
        assert levels[0].to_dict() == expected
        expected = {
            DTTM_ALIAS: {"a1": 600, "b1": 600, "c1": 600},
            "metric1": {"a1": 6, "b1": 15, "c1": 24},
            "metric2": {"a1": 60, "b1": 150, "c1": 240},
            "metric3": {"a1": 600, "b1": 1500, "c1": 2400},
        }
        assert levels[1].to_dict() == expected
        assert levels[2].index.names == ["groupA", "groupB"]
        assert levels[3].index.names == ["groupA", "groupB", "groupC"]
        time_op = "agg_mean"
        levels = test_viz.levels_for(time_op, groups, df)
        assert len(levels) == 4
        expected = {
            DTTM_ALIAS: 200.0,
            "metric1": 5.0,
            "metric2": 50.0,
            "metric3": 500.0,
        }
        assert levels[0].to_dict() == expected
        expected = {
            DTTM_ALIAS: {"a1": 200, "c1": 200, "b1": 200},
            "metric1": {"a1": 2, "b1": 5, "c1": 8},
            "metric2": {"a1": 20, "b1": 50, "c1": 80},
            "metric3": {"a1": 200, "b1": 500, "c1": 800},
        }
        assert levels[1].to_dict() == expected
        assert levels[2].index.names == ["groupA", "groupB"]
        assert levels[3].index.names == ["groupA", "groupB", "groupC"]

    async def test_levels_for_diff_computes_difference(self) -> None:
        raw: dict[Any, Any] = {}
        raw[DTTM_ALIAS] = [100, 200, 300, 100, 200, 300, 100, 200, 300]
        raw["groupA"] = ["a1", "a1", "a1", "b1", "b1", "b1", "c1", "c1", "c1"]
        raw["groupB"] = ["a2", "a2", "a2", "b2", "b2", "b2", "c2", "c2", "c2"]
        raw["groupC"] = ["a3", "a3", "a3", "b3", "b3", "b3", "c3", "c3", "c3"]
        raw["metric1"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        raw["metric2"] = [10, 20, 30, 40, 50, 60, 70, 80, 90]
        raw["metric3"] = [100, 200, 300, 400, 500, 600, 700, 800, 900]
        df = pd.DataFrame(raw)
        groups = ["groupA", "groupB", "groupC"]
        test_viz = viz.PartitionViz(Mock(), {})
        time_op = "point_diff"
        levels = test_viz.levels_for_diff(time_op, groups, df)
        expected = {"metric1": 6, "metric2": 60, "metric3": 600}
        assert levels[0].to_dict() == expected
        expected = {
            "metric1": {"a1": 2, "b1": 2, "c1": 2},
            "metric2": {"a1": 20, "b1": 20, "c1": 20},
            "metric3": {"a1": 200, "b1": 200, "c1": 200},
        }
        assert levels[1].to_dict() == expected
        assert len(levels) == 4
        assert levels[3].index.names == ["groupA", "groupB", "groupC"]

    async def test_levels_for_time_calls_process_data_and_drops_cols(self) -> None:
        raw: dict[Any, Any] = {}
        raw[DTTM_ALIAS] = [100, 200, 300, 100, 200, 300, 100, 200, 300]
        raw["groupA"] = ["a1", "a1", "a1", "b1", "b1", "b1", "c1", "c1", "c1"]
        raw["groupB"] = ["a2", "a2", "a2", "b2", "b2", "b2", "c2", "c2", "c2"]
        raw["groupC"] = ["a3", "a3", "a3", "b3", "b3", "b3", "c3", "c3", "c3"]
        raw["metric1"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        raw["metric2"] = [10, 20, 30, 40, 50, 60, 70, 80, 90]
        raw["metric3"] = [100, 200, 300, 400, 500, 600, 700, 800, 900]
        df = pd.DataFrame(raw)
        groups = ["groupA", "groupB", "groupC"]
        test_viz = viz.PartitionViz(Mock(), {"groupby": groups})

        def return_args(df_drop: pd.DataFrame, aggregate: bool) -> pd.DataFrame:
            return df_drop

        test_viz.process_data = Mock(side_effect=return_args)
        levels = test_viz.levels_for_time(groups, df)
        assert len(levels) == 4
        cols = [DTTM_ALIAS, "metric1", "metric2", "metric3"]
        assert sorted(cols) == sorted(levels[0].columns.tolist())
        cols += ["groupA"]
        assert sorted(cols) == sorted(levels[1].columns.tolist())
        cols += ["groupB"]
        assert sorted(cols) == sorted(levels[2].columns.tolist())
        cols += ["groupC"]
        assert sorted(cols) == sorted(levels[3].columns.tolist())
        assert len(test_viz.process_data.mock_calls) == 4

    async def test_nest_values_returns_hierarchy(self) -> None:
        raw: dict[Any, Any] = {}
        raw["groupA"] = ["a1", "a1", "a1", "b1", "b1", "b1", "c1", "c1", "c1"]
        raw["groupB"] = ["a2", "a2", "a2", "b2", "b2", "b2", "c2", "c2", "c2"]
        raw["groupC"] = ["a3", "a3", "a3", "b3", "b3", "b3", "c3", "c3", "c3"]
        raw["metric1"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        raw["metric2"] = [10, 20, 30, 40, 50, 60, 70, 80, 90]
        raw["metric3"] = [100, 200, 300, 400, 500, 600, 700, 800, 900]
        df = pd.DataFrame(raw)
        test_viz = viz.PartitionViz(Mock(), {})
        groups = ["groupA", "groupB", "groupC"]
        levels = test_viz.levels_for("agg_sum", groups, df)
        nest = test_viz.nest_values(levels)
        assert len(nest) == 3
        for i in range(0, 3):
            assert nest[i]["name"] == "metric" + str(i + 1)
        assert len(nest[0]["children"]) == 3
        assert len(nest[0]["children"][0]["children"]) == 1
        assert len(nest[0]["children"][0]["children"][0]["children"]) == 1

    async def test_nest_values_returns_hierarchy_when_more_dimensions(self) -> None:
        raw: dict[Any, Any] = {}
        raw["category"] = ["a", "a", "a"]
        raw["subcategory"] = ["a.2", "a.1", "a.2"]
        raw["sub_subcategory"] = ["a.2.1", "a.1.1", "a.2.2"]
        raw["metric1"] = [5, 10, 15]
        raw["metric2"] = [50, 100, 150]
        raw["metric3"] = [500, 1000, 1500]
        df = pd.DataFrame(raw)
        test_viz = viz.PartitionViz(Mock(), {})
        groups = ["category", "subcategory", "sub_subcategory"]
        levels = test_viz.levels_for("agg_sum", groups, df)
        nest = test_viz.nest_values(levels)
        assert len(nest) == 3
        for i in range(0, 3):
            assert nest[i]["name"] == "metric" + str(i + 1)
        assert len(nest[0]["children"]) == 1
        assert len(nest[0]["children"][0]["children"]) == 2
        assert len(nest[0]["children"][0]["children"][0]["children"]) == 1
        assert len(nest[0]["children"][0]["children"][1]["children"]) == 2

    async def test_nest_procs_returns_hierarchy(self) -> None:
        raw: dict[Any, Any] = {}
        raw[DTTM_ALIAS] = [100, 200, 300, 100, 200, 300, 100, 200, 300]
        raw["groupA"] = ["a1", "a1", "a1", "b1", "b1", "b1", "c1", "c1", "c1"]
        raw["groupB"] = ["a2", "a2", "a2", "b2", "b2", "b2", "c2", "c2", "c2"]
        raw["groupC"] = ["a3", "a3", "a3", "b3", "b3", "b3", "c3", "c3", "c3"]
        raw["metric1"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        raw["metric2"] = [10, 20, 30, 40, 50, 60, 70, 80, 90]
        raw["metric3"] = [100, 200, 300, 400, 500, 600, 700, 800, 900]
        df = pd.DataFrame(raw)
        test_viz = viz.PartitionViz(Mock(), {})
        groups = ["groupA", "groupB", "groupC"]
        metrics = ["metric1", "metric2", "metric3"]
        procs = {}
        for i in range(0, 4):
            df_drop = df.drop(groups[i:], axis=1)
            pivot = df_drop.pivot_table(
                index=DTTM_ALIAS, columns=groups[:i], values=metrics
            )
            procs[i] = pivot
        nest = test_viz.nest_procs(procs)
        assert len(nest) == 3
        for i in range(0, 3):
            assert nest[i]["name"] == "metric" + str(i + 1)
            assert nest[i].get("val") is None
        assert len(nest[0]["children"]) == 3
        assert len(nest[0]["children"][0]["children"]) == 3
        assert len(nest[0]["children"][0]["children"][0]["children"]) == 1
        assert (
            len(nest[0]["children"][0]["children"][0]["children"][0]["children"]) == 1
        )

    async def test_get_data_calls_correct_method(self) -> None:
        raw: dict[Any, Any] = {}
        raw[DTTM_ALIAS] = [100, 200, 300, 100, 200, 300, 100, 200, 300]
        raw["groupA"] = ["a1", "a1", "a1", "b1", "b1", "b1", "c1", "c1", "c1"]
        raw["groupB"] = ["a2", "a2", "a2", "b2", "b2", "b2", "c2", "c2", "c2"]
        raw["groupC"] = ["a3", "a3", "a3", "b3", "b3", "b3", "c3", "c3", "c3"]
        raw["metric1"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        raw["metric2"] = [10, 20, 30, 40, 50, 60, 70, 80, 90]
        raw["metric3"] = [100, 200, 300, 400, 500, 600, 700, 800, 900]
        df = pd.DataFrame(raw)
        test_viz = viz.PartitionViz(Mock(), {})
        with pytest.raises(ValueError):  # noqa: PT011
            test_viz.get_data(df)
        test_viz.levels_for = Mock(return_value=1)
        test_viz.nest_values = Mock(return_value=1)
        test_viz.form_data["groupby"] = ["groups"]
        test_viz.form_data["time_series_option"] = "not_time"
        test_viz.get_data(df)
        assert test_viz.levels_for.mock_calls[0][1][0] == "agg_sum"
        test_viz.form_data["time_series_option"] = "agg_sum"
        test_viz.get_data(df)
        assert test_viz.levels_for.mock_calls[1][1][0] == "agg_sum"
        test_viz.form_data["time_series_option"] = "agg_mean"
        test_viz.get_data(df)
        assert test_viz.levels_for.mock_calls[2][1][0] == "agg_mean"
        test_viz.form_data["time_series_option"] = "point_diff"
        test_viz.levels_for_diff = Mock(return_value=1)
        test_viz.get_data(df)
        assert test_viz.levels_for_diff.mock_calls[0][1][0] == "point_diff"
        test_viz.form_data["time_series_option"] = "point_percent"
        test_viz.get_data(df)
        assert test_viz.levels_for_diff.mock_calls[1][1][0] == "point_percent"
        test_viz.form_data["time_series_option"] = "point_factor"
        test_viz.get_data(df)
        assert test_viz.levels_for_diff.mock_calls[2][1][0] == "point_factor"
        test_viz.levels_for_time = Mock(return_value=1)
        test_viz.nest_procs = Mock(return_value=1)
        test_viz.form_data["time_series_option"] = "adv_anal"
        test_viz.get_data(df)
        assert len(test_viz.levels_for_time.mock_calls) == 1
        assert len(test_viz.nest_procs.mock_calls) == 1
        test_viz.form_data["time_series_option"] = "time_series"
        test_viz.get_data(df)
        assert test_viz.levels_for.mock_calls[3][1][0] == "agg_sum"
        assert len(test_viz.nest_values.mock_calls) == 7


# ---------------------------------------------------------------------------
# TestRoseVis
# ---------------------------------------------------------------------------


class TestRoseVis:
    async def test_rose_vis_get_data(self) -> None:
        raw: dict[Any, Any] = {}
        t1 = pd.Timestamp("2000")
        t2 = pd.Timestamp("2002")
        t3 = pd.Timestamp("2004")
        raw[DTTM_ALIAS] = [t1, t2, t3, t1, t2, t3, t1, t2, t3]
        raw["groupA"] = ["a1", "a1", "a1", "b1", "b1", "b1", "c1", "c1", "c1"]
        raw["groupB"] = ["a2", "a2", "a2", "b2", "b2", "b2", "c2", "c2", "c2"]
        raw["groupC"] = ["a3", "a3", "a3", "b3", "b3", "b3", "c3", "c3", "c3"]
        raw["metric1"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        df = pd.DataFrame(raw)
        fd = {"metrics": ["metric1"], "groupby": ["groupA"]}
        test_viz = viz.RoseViz(Mock(), fd)
        test_viz.metrics = fd["metrics"]
        res = test_viz.get_data(df)
        expected = {
            946684800000000000: [
                {"time": t1, "value": 1, "key": ("a1",), "name": ("a1",)},
                {"time": t1, "value": 4, "key": ("b1",), "name": ("b1",)},
                {"time": t1, "value": 7, "key": ("c1",), "name": ("c1",)},
            ],
            1009843200000000000: [
                {"time": t2, "value": 2, "key": ("a1",), "name": ("a1",)},
                {"time": t2, "value": 5, "key": ("b1",), "name": ("b1",)},
                {"time": t2, "value": 8, "key": ("c1",), "name": ("c1",)},
            ],
            1072915200000000000: [
                {"time": t3, "value": 3, "key": ("a1",), "name": ("a1",)},
                {"time": t3, "value": 6, "key": ("b1",), "name": ("b1",)},
                {"time": t3, "value": 9, "key": ("c1",), "name": ("c1",)},
            ],
        }
        assert res == expected


# ---------------------------------------------------------------------------
# TestTimeSeriesTableViz
# ---------------------------------------------------------------------------


class TestTimeSeriesTableViz:
    async def test_get_data_metrics(self) -> None:
        form_data = {"metrics": ["sum__A", "count"], "groupby": []}
        datasource = get_datasource_mock()
        raw: dict[Any, Any] = {}
        t1 = pd.Timestamp("2000")
        t2 = pd.Timestamp("2002")
        raw[DTTM_ALIAS] = [t1, t2]
        raw["sum__A"] = [15, 20]
        raw["count"] = [6, 7]
        df = pd.DataFrame(raw)
        test_viz = viz.TimeTableViz(datasource, form_data)
        data = test_viz.get_data(df)
        assert {"count", "sum__A"} == set(data["columns"])
        time_format = "%Y-%m-%d %H:%M:%S"
        expected = {
            t1.strftime(time_format): {"sum__A": 15, "count": 6},
            t2.strftime(time_format): {"sum__A": 20, "count": 7},
        }
        assert data["records"] == expected

    async def test_get_data_group_by(self) -> None:
        form_data = {"metrics": ["sum__A"], "groupby": ["groupby1"]}
        datasource = get_datasource_mock()
        raw: dict[Any, Any] = {}
        t1 = pd.Timestamp("2000")
        t2 = pd.Timestamp("2002")
        raw[DTTM_ALIAS] = [t1, t1, t1, t2, t2, t2]
        raw["sum__A"] = [15, 20, 25, 30, 35, 40]
        raw["groupby1"] = ["a1", "a2", "a3", "a1", "a2", "a3"]
        df = pd.DataFrame(raw)
        test_viz = viz.TimeTableViz(datasource, form_data)
        data = test_viz.get_data(df)
        assert {"a1", "a2", "a3"} == set(data["columns"])
        time_format = "%Y-%m-%d %H:%M:%S"
        expected = {
            t1.strftime(time_format): {"a1": 15, "a2": 20, "a3": 25},
            t2.strftime(time_format): {"a1": 30, "a2": 35, "a3": 40},
        }
        assert data["records"] == expected

    @patch("superset.viz.BaseViz.query_obj")
    async def test_query_obj_throws_metrics_and_groupby(
        self, super_query_obj: Mock
    ) -> None:
        datasource = get_datasource_mock()
        form_data: dict[str, Any] = {"groupby": ["a"]}
        super_query_obj.return_value = {}
        test_viz = viz.TimeTableViz(datasource, form_data)
        with pytest.raises(Exception):  # noqa: B017, PT011
            test_viz.query_obj()
        form_data["metrics"] = ["x", "y"]
        test_viz = viz.TimeTableViz(datasource, form_data)
        with pytest.raises(Exception):  # noqa: B017, PT011
            test_viz.query_obj()

    async def test_query_obj_order_by(self) -> None:
        test_viz = viz.TimeTableViz(
            get_datasource_mock(), {"metrics": ["sum__A", "count"], "groupby": []}
        )
        query_obj = test_viz.query_obj()
        assert query_obj["orderby"] == [("sum__A", False)]


# ---------------------------------------------------------------------------
# TestBaseDeckGLViz
# ---------------------------------------------------------------------------


class TestBaseDeckGLViz:
    async def test_get_metrics(self) -> None:
        form_data = dict(DECK_PATH_FORM_DATA)
        datasource = get_datasource_mock()
        test_viz_deckgl = viz.BaseDeckGLViz(datasource, form_data)
        result = test_viz_deckgl.get_metrics()
        assert result == [form_data.get("size")]

        form_data = {}
        test_viz_deckgl = viz.BaseDeckGLViz(datasource, form_data)
        result = test_viz_deckgl.get_metrics()
        assert result == []

    async def test_scatterviz_get_metrics(self) -> None:
        datasource = get_datasource_mock()

        form_data: dict[str, Any] = {}
        test_viz_deckgl = viz.DeckScatterViz(datasource, form_data)
        test_viz_deckgl.point_radius_fixed = {"type": "metric", "value": "int"}
        result = test_viz_deckgl.get_metrics()
        assert result == ["int"]

        form_data = {}
        test_viz_deckgl = viz.DeckScatterViz(datasource, form_data)
        test_viz_deckgl.point_radius_fixed = {}
        result = test_viz_deckgl.get_metrics()
        assert result == []

    async def test_get_js_columns(self) -> None:
        form_data = dict(DECK_PATH_FORM_DATA)
        datasource = get_datasource_mock()
        mock_d = {"a": "dummy1", "b": "dummy2", "c": "dummy3"}
        test_viz_deckgl = viz.BaseDeckGLViz(datasource, form_data)
        result = test_viz_deckgl.get_js_columns(mock_d)
        assert result == {"color": None}

    async def test_get_properties(self) -> None:
        mock_d: dict[str, Any] = {}
        form_data = dict(DECK_PATH_FORM_DATA)
        datasource = get_datasource_mock()
        test_viz_deckgl = viz.BaseDeckGLViz(datasource, form_data)

        with pytest.raises(NotImplementedError) as context:
            test_viz_deckgl.get_properties(mock_d)
        assert "" in str(context.value)

    async def test_process_spatial_query_obj(self) -> None:
        form_data = dict(DECK_PATH_FORM_DATA)
        datasource = get_datasource_mock()
        mock_key = "spatial_key"
        mock_gb: list[str] = []
        test_viz_deckgl = viz.BaseDeckGLViz(datasource, form_data)

        with pytest.raises(ValueError) as context:  # noqa: PT011
            test_viz_deckgl.process_spatial_query_obj(mock_key, mock_gb)
        assert "Bad spatial key" in str(context.value)

        test_form_data = {
            "latlong_key": {"type": "latlong", "lonCol": "lon", "latCol": "lat"},
            "delimited_key": {"type": "delimited", "lonlatCol": "lonlat"},
            "geohash_key": {"type": "geohash", "geohashCol": "geo"},
        }

        datasource = get_datasource_mock()
        expected_results = {
            "latlong_key": ["lon", "lat"],
            "delimited_key": ["lonlat"],
            "geohash_key": ["geo"],
        }
        for mock_key in ["latlong_key", "delimited_key", "geohash_key"]:
            mock_gb = []
            test_viz_deckgl = viz.BaseDeckGLViz(datasource, test_form_data)
            test_viz_deckgl.process_spatial_query_obj(mock_key, mock_gb)
            assert expected_results.get(mock_key) == mock_gb

    async def test_geojson_query_obj(self) -> None:
        form_data = dict(DECK_GEOJSON_FORM_DATA)
        datasource = get_datasource_mock()
        test_viz_deckgl = viz.DeckGeoJson(datasource, form_data)
        results = test_viz_deckgl.query_obj()

        assert results["metrics"] == []
        assert results["groupby"] == []
        assert results["columns"] == ["test_col"]

    async def test_parse_coordinates(self) -> None:
        form_data = dict(DECK_PATH_FORM_DATA)
        datasource = get_datasource_mock()
        viz_instance = viz.BaseDeckGLViz(datasource, form_data)

        coord = viz_instance.parse_coordinates("1.23, 3.21")
        assert coord == (1.23, 3.21)

        coord = viz_instance.parse_coordinates("1.23 3.21")
        assert coord == (1.23, 3.21)

        assert viz_instance.parse_coordinates(None) is None
        assert viz_instance.parse_coordinates("") is None

    async def test_parse_coordinates_raises(self) -> None:
        form_data = dict(DECK_PATH_FORM_DATA)
        datasource = get_datasource_mock()
        test_viz_deckgl = viz.BaseDeckGLViz(datasource, form_data)

        with pytest.raises(SpatialException):
            test_viz_deckgl.parse_coordinates("NULL")

        with pytest.raises(SpatialException):
            test_viz_deckgl.parse_coordinates("fldkjsalkj,fdlaskjfjadlksj")

    async def test_filter_nulls(self) -> None:
        test_form_data = {
            "latlong_key": {"type": "latlong", "lonCol": "lon", "latCol": "lat"},
            "delimited_key": {"type": "delimited", "lonlatCol": "lonlat"},
            "geohash_key": {"type": "geohash", "geohashCol": "geo"},
        }

        datasource = get_datasource_mock()
        expected_results = {
            "latlong_key": [
                {
                    "clause": "WHERE",
                    "expressionType": "SIMPLE",
                    "filterOptionName": "c7f171cf3204bcbf456acfeac5cd9afd",
                    "comparator": "",
                    "operator": "IS NOT NULL",
                    "subject": "lat",
                },
                {
                    "clause": "WHERE",
                    "expressionType": "SIMPLE",
                    "filterOptionName": "52634073fbb8ae0a3aa59ad48abac55e",
                    "comparator": "",
                    "operator": "IS NOT NULL",
                    "subject": "lon",
                },
            ],
            "delimited_key": [
                {
                    "clause": "WHERE",
                    "expressionType": "SIMPLE",
                    "filterOptionName": "cae5c925c140593743da08499e6fb207",
                    "comparator": "",
                    "operator": "IS NOT NULL",
                    "subject": "lonlat",
                }
            ],
            "geohash_key": [
                {
                    "clause": "WHERE",
                    "expressionType": "SIMPLE",
                    "filterOptionName": "d84f55222d8e414e888fa5f990b341d2",
                    "comparator": "",
                    "operator": "IS NOT NULL",
                    "subject": "geo",
                }
            ],
        }
        for mock_key in ["latlong_key", "delimited_key", "geohash_key"]:
            test_viz_deckgl = viz.BaseDeckGLViz(datasource, test_form_data.copy())
            test_viz_deckgl.spatial_control_keys = [mock_key]
            test_viz_deckgl.add_null_filters()
            adhoc_filters = test_viz_deckgl.form_data["adhoc_filters"]
            assert expected_results.get(mock_key) == adhoc_filters

    async def test_init_with_layer_filtering_applied(self) -> None:
        datasource = get_datasource_mock()
        form_data = {
            "slice_id": 123,
            "adhoc_filters": [
                {
                    "clause": "WHERE",
                    "subject": "col1",
                    "operator": "==",
                    "comparator": "value1",
                    "layerFilterScope": [0, 1],
                    "deck_slices": [123, 456],
                },
                {
                    "clause": "WHERE",
                    "subject": "col2",
                    "operator": "!=",
                    "comparator": "value2",
                    "layerFilterScope": [1],
                    "deck_slices": [123, 456],
                },
            ],
        }

        test_viz = viz.BaseDeckGLViz(datasource, form_data)
        assert len(test_viz.form_data["adhoc_filters"]) == 1
        assert test_viz.form_data["adhoc_filters"][0]["subject"] == "col1"

    async def test_init_without_layer_filtering(self) -> None:
        datasource = get_datasource_mock()
        form_data = {
            "adhoc_filters": [
                {
                    "clause": "WHERE",
                    "subject": "col1",
                    "operator": "==",
                    "comparator": "value1",
                }
            ]
        }
        original_filters = form_data["adhoc_filters"].copy()

        test_viz = viz.BaseDeckGLViz(datasource, form_data)
        assert test_viz.form_data["adhoc_filters"] == original_filters

    async def test_should_apply_layer_filtering_true(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {"slice_id": 123, "adhoc_filters": [{"layerFilterScope": [0, 1]}]}
        result = test_viz._should_apply_layer_filtering(form_data)
        assert result is True

    async def test_should_apply_layer_filtering_false_missing_slice_id(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {"adhoc_filters": [{"layerFilterScope": [0, 1]}]}
        result = test_viz._should_apply_layer_filtering(form_data)
        assert result is False

    async def test_should_apply_layer_filtering_false_missing_adhoc_filters(
        self,
    ) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {"slice_id": 123}
        result = test_viz._should_apply_layer_filtering(form_data)
        assert result is False

    async def test_should_apply_layer_filtering_false_no_layer_scoped_filters(
        self,
    ) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {
            "slice_id": 123,
            "adhoc_filters": [{"clause": "WHERE", "subject": "col1"}],
        }
        result = test_viz._should_apply_layer_filtering(form_data)
        assert result is False

    async def test_has_layer_scoped_filters_true_with_dict(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {
            "adhoc_filters": [{"layerFilterScope": [0, 1]}, {"clause": "WHERE"}]
        }
        result = test_viz._has_layer_scoped_filters(form_data)
        assert result is True

    async def test_has_layer_scoped_filters_true_with_non_none_value(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {
            "adhoc_filters": [
                {"layerFilterScope": []},
                {"clause": "WHERE"},
            ]
        }
        result = test_viz._has_layer_scoped_filters(form_data)
        assert result is True

    async def test_has_layer_scoped_filters_false_none_value(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {"adhoc_filters": [{"layerFilterScope": None}, {"clause": "WHERE"}]}
        result = test_viz._has_layer_scoped_filters(form_data)
        assert result is False

    async def test_has_layer_scoped_filters_false_no_scoped_filters(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {
            "adhoc_filters": [
                {"clause": "WHERE", "subject": "col1"},
                {"clause": "WHERE", "subject": "col2"},
            ]
        }
        result = test_viz._has_layer_scoped_filters(form_data)
        assert result is False

    async def test_has_layer_scoped_filters_empty_filters(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data: dict[str, Any] = {"adhoc_filters": []}
        result = test_viz._has_layer_scoped_filters(form_data)
        assert result is False

    async def test_apply_multilayer_filtering_filters_by_layer_scope(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {
            "slice_id": 456,
            "adhoc_filters": [
                {
                    "subject": "global_filter",
                    "deck_slices": [123, 456],
                },
                {
                    "subject": "layer_0_filter",
                    "layerFilterScope": [0],
                    "deck_slices": [123, 456],
                },
                {
                    "subject": "layer_1_filter",
                    "layerFilterScope": [1],
                    "deck_slices": [123, 456],
                },
                {
                    "subject": "layer_0_1_filter",
                    "layerFilterScope": [0, 1],
                    "deck_slices": [123, 456],
                },
            ],
        }

        result = test_viz._apply_multilayer_filtering(form_data)
        assert len(result["adhoc_filters"]) == 3
        subjects = [flt["subject"] for flt in result["adhoc_filters"]]
        assert "global_filter" in subjects
        assert "layer_1_filter" in subjects
        assert "layer_0_1_filter" in subjects
        assert "layer_0_filter" not in subjects

    async def test_apply_multilayer_filtering_no_deck_slices(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {"slice_id": 123, "adhoc_filters": [{"subject": "filter1"}]}
        result = test_viz._apply_multilayer_filtering(form_data)
        assert result == form_data

    async def test_apply_multilayer_filtering_slice_not_in_deck_slices(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {
            "slice_id": 999,
            "adhoc_filters": [{"subject": "filter1", "deck_slices": [123, 456]}],
        }
        result = test_viz._apply_multilayer_filtering(form_data)
        assert result == form_data

    async def test_get_deck_slices_from_filters_found(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {
            "adhoc_filters": [
                {"subject": "filter1"},
                {"subject": "filter2", "deck_slices": [123, 456, 789]},
                {"subject": "filter3"},
            ]
        }
        result = test_viz._get_deck_slices_from_filters(form_data)
        assert result == [123, 456, 789]

    async def test_get_deck_slices_from_filters_not_found(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data = {"adhoc_filters": [{"subject": "filter1"}, {"subject": "filter2"}]}
        result = test_viz._get_deck_slices_from_filters(form_data)
        assert result is None

    async def test_get_deck_slices_from_filters_empty_filters(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        form_data: dict[str, Any] = {"adhoc_filters": []}
        result = test_viz._get_deck_slices_from_filters(form_data)
        assert result is None

    async def test_get_filter_layer_scope_dict(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        filter_item = {"layerFilterScope": [0, 1, 2]}
        result = test_viz._get_filter_layer_scope(filter_item)
        assert result == [0, 1, 2]

    async def test_get_filter_layer_scope_dict_none(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        filter_item = {"layerFilterScope": None}
        result = test_viz._get_filter_layer_scope(filter_item)
        assert result is None

    async def test_get_filter_layer_scope_dict_missing_key(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        filter_item = {"subject": "col1"}
        result = test_viz._get_filter_layer_scope(filter_item)
        assert result is None

    async def test_get_filter_layer_scope_object_with_attribute(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        filter_item = Mock()
        filter_item.layerFilterScope = [1, 2]
        result = test_viz._get_filter_layer_scope(filter_item)
        assert result == [1, 2]

    async def test_get_filter_layer_scope_object_without_attribute(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        filter_item = Mock()
        del filter_item.layerFilterScope
        result = test_viz._get_filter_layer_scope(filter_item)
        assert result is None

    async def test_get_filter_layer_scope_non_dict_non_object(self) -> None:
        datasource = get_datasource_mock()
        test_viz = viz.BaseDeckGLViz(datasource, {})

        result = test_viz._get_filter_layer_scope("string_filter")
        assert result is None

        result = test_viz._get_filter_layer_scope(123)
        assert result is None

        result = test_viz._get_filter_layer_scope(None)
        assert result is None


# ---------------------------------------------------------------------------
# TestDeckGLMultiLayer
# ---------------------------------------------------------------------------


class TestDeckGLMultiLayer:
    async def test_filter_items_by_scope_with_filter_id(self) -> None:
        datasource = get_datasource_mock()
        form_data: dict[str, Any] = {}
        test_viz = viz.DeckGLMultiLayer(datasource, form_data)

        filter_item_1 = Mock()
        filter_item_1.filterId = "filter_1"
        filter_item_2 = Mock()
        filter_item_2.filterId = "filter_2"
        filter_item_3 = Mock()
        filter_item_3.filterId = "filter_3"

        items = [filter_item_1, filter_item_2, filter_item_3]
        layer_index = 0
        layer_filter_scope = {"filter_1": [0, 1], "filter_2": [1], "filter_3": []}

        result = test_viz._filter_items_by_scope(items, layer_index, layer_filter_scope)
        assert len(result) == 2
        assert filter_item_1 in result
        assert filter_item_3 in result
        assert filter_item_2 not in result

    async def test_filter_items_by_scope_without_filter_id(self) -> None:
        datasource = get_datasource_mock()
        form_data: dict[str, Any] = {}
        test_viz = viz.DeckGLMultiLayer(datasource, form_data)

        filter_item_1 = Mock()
        del filter_item_1.filterId
        filter_item_2 = Mock()
        filter_item_2.filterId = None

        items = [filter_item_1, filter_item_2]
        layer_index = 0
        layer_filter_scope = {"filter_1": [1]}

        result = test_viz._filter_items_by_scope(items, layer_index, layer_filter_scope)
        assert len(result) == 2
        assert filter_item_1 in result
        assert filter_item_2 in result

    async def test_process_extra_form_data_filters(self) -> None:
        datasource = get_datasource_mock()
        form_data: dict[str, Any] = {}
        test_viz = viz.DeckGLMultiLayer(datasource, form_data)

        layer_index = 0
        layer_filter_scope = {"filter_1": [0, 1], "filter_2": [1], "filter_3": []}
        filter_data_mapping = {
            "filter_1": [{"column": "col1", "op": "==", "val": "value1"}],
            "filter_2": [{"column": "col2", "op": "!=", "val": "value2"}],
            "filter_3": [{"column": "col3", "op": ">", "val": 100}],
        }
        extra_form_data = {"existing_key": "existing_value"}

        result = test_viz._process_extra_form_data_filters(
            layer_index, layer_filter_scope, filter_data_mapping, extra_form_data
        )

        expected_filters = [
            {"column": "col1", "op": "==", "val": "value1"},
            {"column": "col3", "op": ">", "val": 100},
        ]
        assert result["filters"] == expected_filters
        assert result["existing_key"] == "existing_value"

    async def test_process_extra_form_data_filters_empty_inputs(self) -> None:
        datasource = get_datasource_mock()
        form_data: dict[str, Any] = {}
        test_viz = viz.DeckGLMultiLayer(datasource, form_data)

        result = test_viz._process_extra_form_data_filters(0, {}, {}, {})
        assert result == {}

        extra_form_data = {"key": "value"}
        result = test_viz._process_extra_form_data_filters(0, {}, {}, extra_form_data)
        assert result == extra_form_data

    async def test_apply_layer_filtering_without_layer_filter_scope(self) -> None:
        datasource = get_datasource_mock()
        form_data = {
            "extra_filters": [Mock(), Mock()],
            "adhoc_filters": [Mock()],
            "extra_form_data": {"key": "value"},
        }
        test_viz = viz.DeckGLMultiLayer(datasource, form_data)

        layer_form_data = {"viz_type": "deck_scatter"}
        layer_index = 0

        result = test_viz._apply_layer_filtering(layer_form_data, layer_index)
        assert result["extra_filters"] == form_data["extra_filters"]
        assert result["adhoc_filters"] == form_data["adhoc_filters"]
        assert result["extra_form_data"] == form_data["extra_form_data"]

    async def test_apply_layer_filtering_with_layer_filter_scope(self) -> None:
        datasource = get_datasource_mock()

        extra_filter_1 = Mock()
        extra_filter_1.filterId = "filter_1"
        extra_filter_2 = Mock()
        extra_filter_2.filterId = "filter_2"

        adhoc_filter_1 = Mock()
        adhoc_filter_1.filterId = "filter_1"

        form_data = {
            "layer_filter_scope": {"filter_1": [0], "filter_2": [1]},
            "filter_data_mapping": {
                "filter_1": [{"column": "col1", "op": "==", "val": "value1"}]
            },
            "extra_filters": [extra_filter_1, extra_filter_2],
            "adhoc_filters": [adhoc_filter_1],
            "extra_form_data": {"existing": "data"},
        }
        test_viz = viz.DeckGLMultiLayer(datasource, form_data)

        layer_form_data = {"viz_type": "deck_scatter"}
        layer_index = 0

        result = test_viz._apply_layer_filtering(layer_form_data, layer_index)

        assert result["extra_filters"][0].filterId == "filter_1"
        assert len(result["adhoc_filters"]) == 1
        assert result["adhoc_filters"][0].filterId == "filter_1"
        assert result["extra_form_data"]["filters"] == [
            {"column": "col1", "op": "==", "val": "value1"}
        ]

    async def test_get_data_with_layer_filtering(self, db_session: Any) -> None:
        """Port of the upstream layer-filtering payload test.

        The Liteset port's real entry point is the async ``async_get_data``,
        which loads sub-layer slices from the DB and runs their viz classes.
        We seed two real ``Slice`` rows (deck_scatter / deck_path), patch the
        viz registry to mock viz classes (as upstream did), and assert the
        per-layer filtering hook and the mapbox-keyed payload shape.
        """
        settings = SupersetSettings(mapbox_api_key="test_key")  # type: ignore[call-arg]
        datasource = get_datasource_mock()

        database = await f.create_database(
            db_session, database_name="deck_multi_db_1"
        )
        ds = await f.create_dataset(
            db_session, table_name="deck_multi_ds_1", database=database
        )
        slice_1 = await f.create_chart(
            db_session,
            slice_name="Layer 1",
            viz_type="deck_scatter",
            datasource_id=ds.id,
            params='{"viz_type": "deck_scatter", "layer_name": "Layer 1"}',
        )
        slice_2 = await f.create_chart(
            db_session,
            slice_name="Layer 2",
            viz_type="deck_path",
            datasource_id=ds.id,
            params='{"viz_type": "deck_path", "layer_name": "Layer 2"}',
        )

        mock_scatter_viz_class = Mock()
        mock_scatter_viz_instance = Mock()
        mock_scatter_viz_instance.get_payload = AsyncMock(
            return_value={"data": {"features": [{"id": 1}]}}
        )
        mock_scatter_viz_class.return_value = mock_scatter_viz_instance

        mock_path_viz_class = Mock()
        mock_path_viz_instance = Mock()
        mock_path_viz_instance.get_payload = AsyncMock(
            return_value={"data": {"features": [{"id": 2}]}}
        )
        mock_path_viz_class.return_value = mock_path_viz_instance

        viz_type_map = {
            "deck_scatter": mock_scatter_viz_class,
            "deck_path": mock_path_viz_class,
        }

        form_data = {
            "layer_filter_scope": {"filter_1": [0], "filter_2": [1]},
            "filter_data_mapping": {
                "filter_1": [{"column": "col1", "op": "==", "val": "value1"}],
                "filter_2": [{"column": "col2", "op": "!=", "val": "value2"}],
            },
            "deck_slices": [slice_1.id, slice_2.id],
        }

        test_viz = viz.DeckGLMultiLayer(datasource, form_data, settings=settings)
        test_viz._apply_layer_filtering = Mock(
            side_effect=lambda fd, idx: fd
        )

        with patch.object(viz, "viz_types", viz_type_map):
            result = await test_viz.async_get_data(pd.DataFrame(), db_session)

        # Both layers should have been pushed through the per-layer filter hook,
        # and -- like upstream's assert_any_call(slice_N.form_data, idx) -- each
        # specific slice's form_data must be routed to the correct layer index.
        # The port loads the sub-layer slices from the DB (whose order is not
        # guaranteed), so we resolve the expected index from the actual load
        # order and assert the form_data->index correspondence per slice.
        assert test_viz._apply_layer_filtering.call_count == 2

        stmt = select(Slice).where(Slice.id.in_([slice_1.id, slice_2.id]))
        loaded = (await db_session.execute(stmt)).scalars().all()
        expected_index = {slc.id: idx for idx, slc in enumerate(loaded)}

        # Map each recorded call's form_data (identified by slice_id) to the
        # layer index it was passed with.
        called_index_by_slice = {
            call.args[0]["slice_id"]: call.args[1]
            for call in test_viz._apply_layer_filtering.mock_calls
        }
        assert called_index_by_slice[slice_1.id] == expected_index[slice_1.id]
        assert called_index_by_slice[slice_2.id] == expected_index[slice_2.id]

        assert isinstance(result, dict)
        assert "features" in result
        assert "mapboxApiKey" in result
        assert "slices" in result
        assert result["mapboxApiKey"] == "test_key"

    async def test_get_data_filters_none_data_slices(self, db_session: Any) -> None:
        """Slices whose ``data`` is None are dropped from the payload's slices."""
        settings = SupersetSettings(mapbox_api_key="test_key")  # type: ignore[call-arg]
        datasource = get_datasource_mock()

        database = await f.create_database(
            db_session, database_name="deck_multi_db_2"
        )
        ds = await f.create_dataset(
            db_session, table_name="deck_multi_ds_2", database=database
        )
        slice_1 = await f.create_chart(
            db_session,
            slice_name="Has Data",
            viz_type="deck_scatter",
            datasource_id=ds.id,
            params='{"viz_type": "deck_scatter"}',
        )
        slice_2 = await f.create_chart(
            db_session,
            slice_name="None Data",
            viz_type="deck_path",
            datasource_id=ds.id,
            params='{"viz_type": "deck_path"}',
        )

        mock_viz_class = Mock()
        mock_viz_instance = Mock()
        mock_viz_instance.get_payload = AsyncMock(
            return_value={"data": {"features": []}}
        )
        mock_viz_class.return_value = mock_viz_instance

        viz_type_map = {"deck_scatter": mock_viz_class, "deck_path": mock_viz_class}

        form_data = {"deck_slices": [slice_1.id, slice_2.id]}

        test_viz = viz.DeckGLMultiLayer(datasource, form_data, settings=settings)

        # Force slice_2 to report ``None`` data so it is filtered from
        # ``result["slices"]`` while slice_1 keeps a real ``.data`` dict.
        none_data_id = slice_2.id

        def patched_data(self: Any) -> Any:
            return None if self.id == none_data_id else {"features": []}

        with (
            patch.object(viz, "viz_types", viz_type_map),
            patch.object(type(slice_1), "data", property(patched_data)),
        ):
            result = await test_viz.async_get_data(pd.DataFrame(), db_session)
            # Capture slice_1's patched ``.data`` while the patch is active so the
            # identity check below survives patch teardown.
            slice_1_data = slice_1.data

        assert isinstance(result, dict)
        assert len(result["slices"]) == 1
        # The surviving slice is specifically slice_1 (the one whose ``.data`` is
        # non-None). slice_1's patched ``data`` property returns ``{"features": []}``
        # (the seam payload differs from upstream's ``{"type": "Feature"}`` only
        # because the port reads ``.data`` from a real ORM instance).
        assert result["slices"][0] == slice_1_data

    async def test_get_data_empty_deck_slices(self, db_session: Any) -> None:
        """No ``deck_slices`` -> empty features / slices, mapbox key passthrough."""
        settings = SupersetSettings(mapbox_api_key="test_key")  # type: ignore[call-arg]
        datasource = get_datasource_mock()
        form_data: dict[str, Any] = {"deck_slices": []}

        test_viz = viz.DeckGLMultiLayer(datasource, form_data, settings=settings)
        result = await test_viz.async_get_data(pd.DataFrame(), db_session)

        assert isinstance(result, dict)
        assert result["features"] == {}
        assert result["slices"] == []
        assert result["mapboxApiKey"] == "test_key"


# ---------------------------------------------------------------------------
# TestTimeSeriesViz
# ---------------------------------------------------------------------------


class TestTimeSeriesViz:
    async def test_timeseries_unicode_data(self) -> None:
        datasource = get_datasource_mock()
        form_data = {"groupby": ["name"], "metrics": ["sum__payout"]}
        raw: dict[Any, Any] = {}
        raw["name"] = [
            "Real Madrid C.F.\U0001f1fa\U0001f1f8\U0001f1ec\U0001f1e7",
            "Real Madrid C.F.\U0001f1fa\U0001f1f8\U0001f1ec\U0001f1e7",
            "Real Madrid Basket",
            "Real Madrid Basket",
        ]
        raw["__timestamp"] = [
            "2018-02-20T00:00:00",
            "2018-03-09T00:00:00",
            "2018-02-20T00:00:00",
            "2018-03-09T00:00:00",
        ]
        raw["sum__payout"] = [2, 2, 4, 4]
        df = pd.DataFrame(raw)

        test_viz = viz.NVD3TimeSeriesViz(datasource, form_data)
        viz_data = test_viz.get_data(df)
        expected = [
            {
                "values": [
                    {"y": 4, "x": "2018-02-20T00:00:00"},
                    {"y": 4, "x": "2018-03-09T00:00:00"},
                ],
                "key": ("Real Madrid Basket",),
            },
            {
                "values": [
                    {"y": 2, "x": "2018-02-20T00:00:00"},
                    {"y": 2, "x": "2018-03-09T00:00:00"},
                ],
                "key": ("Real Madrid C.F.\U0001f1fa\U0001f1f8\U0001f1ec\U0001f1e7",),
            },
        ]
        assert viz_data == expected

    async def test_process_data_resample(self) -> None:
        datasource = get_datasource_mock()

        df = pd.DataFrame(
            {
                "__timestamp": pd.to_datetime(
                    ["2019-01-01", "2019-01-02", "2019-01-05", "2019-01-07"]
                ),
                "y": [1.0, 2.0, 5.0, 7.0],
            }
        )

        assert viz.NVD3TimeSeriesViz(
            datasource,
            {"metrics": ["y"], "resample_method": "sum", "resample_rule": "1D"},
        ).process_data(df)["y"].tolist() == [1.0, 2.0, 0.0, 0.0, 5.0, 0.0, 7.0]

        np.testing.assert_equal(
            viz.NVD3TimeSeriesViz(
                datasource,
                {"metrics": ["y"], "resample_method": "asfreq", "resample_rule": "1D"},
            )
            .process_data(df)["y"]
            .tolist(),
            [1.0, 2.0, np.nan, np.nan, 5.0, np.nan, 7.0],
        )

    async def test_apply_rolling(self) -> None:
        datasource = get_datasource_mock()
        df = pd.DataFrame(
            index=pd.to_datetime(
                ["2019-01-01", "2019-01-02", "2019-01-05", "2019-01-07"]
            ),
            data={"y": [1.0, 2.0, 3.0, 4.0]},
        )
        assert viz.NVD3TimeSeriesViz(
            datasource,
            {
                "metrics": ["y"],
                "rolling_type": "cumsum",
                "rolling_periods": 0,
                "min_periods": 0,
            },
        ).apply_rolling(df)["y"].tolist() == [1.0, 3.0, 6.0, 10.0]
        assert viz.NVD3TimeSeriesViz(
            datasource,
            {
                "metrics": ["y"],
                "rolling_type": "sum",
                "rolling_periods": 2,
                "min_periods": 0,
            },
        ).apply_rolling(df)["y"].tolist() == [1.0, 3.0, 5.0, 7.0]
        assert viz.NVD3TimeSeriesViz(
            datasource,
            {
                "metrics": ["y"],
                "rolling_type": "mean",
                "rolling_periods": 10,
                "min_periods": 0,
            },
        ).apply_rolling(df)["y"].tolist() == [1.0, 1.5, 2.0, 2.5]

    async def test_apply_rolling_without_data(self) -> None:
        datasource = get_datasource_mock()
        df = pd.DataFrame(
            index=pd.to_datetime(
                ["2019-01-01", "2019-01-02", "2019-01-05", "2019-01-07"]
            ),
            data={"y": [1.0, 2.0, 3.0, 4.0]},
        )
        test_viz = viz.NVD3TimeSeriesViz(
            datasource,
            {
                "metrics": ["y"],
                "rolling_type": "cumsum",
                "rolling_periods": 4,
                "min_periods": 4,
            },
        )
        with pytest.raises(QueryObjectValidationError):
            test_viz.apply_rolling(df)
