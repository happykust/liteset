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
"""Unit tests for superset/examples/utils.py parity fixes.

Covers two 1:1 regressions vs. superset_old:
  1. _update_metadata_chart_ids: scope.excluded lists drop unmapped IDs
     (original uses filter-not-keep; liteset wrongly used id_map.get(id, id)).
  2. _import_dataset: existing dataset is updated from config on re-run
     (original calls import_from_dict with overwrite=True which merges all
     fields and syncs columns/metrics; liteset wrongly returned stale object).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Finding 1: scope.excluded — unmapped IDs must be DROPPED, not kept
# ---------------------------------------------------------------------------


def _make_position(entries: list[tuple[str, int, str]]) -> dict:
    """Build a minimal position dict.

    Each entry is (component_key, old_chart_id, uuid_str).
    """
    pos = {}
    for key, chart_id, uuid in entries:
        pos[key] = {
            "type": "CHART",
            "meta": {"chartId": chart_id, "uuid": uuid},
        }
    return pos


def _call_update_metadata(
    metadata: dict,
    position: dict,
    chart_ids: dict,
    dataset_info: dict | None = None,
) -> None:
    from superset.examples.utils import _update_metadata_chart_ids

    _update_metadata_chart_ids(metadata, position, chart_ids, dataset_info or {})


# --- native_filter_configuration scope.excluded ---


def test_native_filter_scope_excluded_drops_unmapped_ids():
    """Unmapped IDs in native_filter scope.excluded are dropped (not kept)."""
    # id_map = {10: 100} — only old_id=10 is mapped; 20 and 30 are not
    position = _make_position([("CHART-1", 10, "uuid-1")])
    chart_ids = {"uuid-1": 100}  # uuid-1 -> new_id 100

    metadata = {"native_filter_configuration": [{"scope": {"excluded": [10, 20, 30]}}]}
    _call_update_metadata(metadata, position, chart_ids)

    excluded = metadata["native_filter_configuration"][0]["scope"]["excluded"]
    # 10 is in id_map -> maps to 100; 20 and 30 are not in id_map -> dropped
    assert excluded == [100], f"Expected [100], got {excluded}"


def test_native_filter_scope_excluded_all_unmapped_gives_empty():
    """When no excluded IDs are in id_map, result is empty list."""
    position = {}  # empty position -> id_map is {}
    chart_ids: dict = {}

    metadata = {"native_filter_configuration": [{"scope": {"excluded": [5, 6, 7]}}]}
    _call_update_metadata(metadata, position, chart_ids)

    excluded = metadata["native_filter_configuration"][0]["scope"]["excluded"]
    assert excluded == [], f"Expected [], got {excluded}"


def test_native_filter_scope_excluded_all_mapped():
    """When all excluded IDs are mapped, all are remapped (no drops)."""
    position = _make_position(
        [
            ("CHART-1", 10, "uuid-1"),
            ("CHART-2", 20, "uuid-2"),
        ]
    )
    chart_ids = {"uuid-1": 100, "uuid-2": 200}

    metadata = {"native_filter_configuration": [{"scope": {"excluded": [10, 20]}}]}
    _call_update_metadata(metadata, position, chart_ids)

    excluded = metadata["native_filter_configuration"][0]["scope"]["excluded"]
    assert set(excluded) == {100, 200}, f"Expected {{100, 200}}, got {excluded}"


# --- global_chart_configuration scope.excluded ---


def test_global_chart_configuration_scope_excluded_drops_unmapped():
    """Unmapped IDs in global_chart_configuration scope.excluded are dropped."""
    position = _make_position([("CHART-1", 10, "uuid-1")])
    chart_ids = {"uuid-1": 100}

    metadata = {"global_chart_configuration": {"scope": {"excluded": [10, 99]}}}
    _call_update_metadata(metadata, position, chart_ids)

    excluded = metadata["global_chart_configuration"]["scope"]["excluded"]
    assert excluded == [100], f"Expected [100], got {excluded}"


def test_global_chart_configuration_scope_excluded_all_unmapped_gives_empty():
    """All-unmapped global_chart_configuration excluded list becomes empty."""
    position = _make_position([("CHART-1", 10, "uuid-1")])
    chart_ids = {"uuid-1": 100}

    metadata = {
        "global_chart_configuration": {
            "scope": {"excluded": [99, 88]}  # neither in id_map
        }
    }
    _call_update_metadata(metadata, position, chart_ids)

    excluded = metadata["global_chart_configuration"]["scope"]["excluded"]
    assert excluded == [], f"Expected [], got {excluded}"


# --- chart_configuration[*].crossFilters.scope.excluded ---


def test_chart_configuration_cross_filter_scope_excluded_drops_unmapped():
    """Unmapped IDs in chart_configuration crossFilters scope.excluded are dropped."""
    position = _make_position(
        [
            ("CHART-1", 10, "uuid-1"),
            ("CHART-2", 20, "uuid-2"),
        ]
    )
    chart_ids = {"uuid-1": 100, "uuid-2": 200}

    metadata = {
        "chart_configuration": {
            "10": {
                "id": 10,
                "crossFilters": {
                    "scope": {
                        "excluded": [20, 30]  # 20 is mapped to 200; 30 is not
                    }
                },
            }
        }
    }
    _call_update_metadata(metadata, position, chart_ids)

    new_cfg = metadata["chart_configuration"]
    # outer key "10" is remapped to "100" since old_id=10 -> new_id=100
    assert "100" in new_cfg, f"Expected key '100', got {list(new_cfg)}"
    excluded = new_cfg["100"]["crossFilters"]["scope"]["excluded"]
    # 20 -> 200 (kept); 30 not in id_map (dropped)
    assert excluded == [200], f"Expected [200], got {excluded}"


def test_chart_configuration_cross_filter_scope_excluded_all_unmapped_empty():
    """All-unmapped crossFilters excluded list becomes empty."""
    position = _make_position([("CHART-1", 10, "uuid-1")])
    chart_ids = {"uuid-1": 100}

    metadata = {
        "chart_configuration": {
            "10": {
                "id": 10,
                "crossFilters": {"scope": {"excluded": [99, 88]}},
            }
        }
    }
    _call_update_metadata(metadata, position, chart_ids)

    new_cfg = metadata["chart_configuration"]
    assert "100" in new_cfg
    excluded = new_cfg["100"]["crossFilters"]["scope"]["excluded"]
    assert excluded == [], f"Expected [], got {excluded}"


# ---------------------------------------------------------------------------
# Finding 2: _import_dataset updates existing dataset on re-run
# ---------------------------------------------------------------------------


def _make_session_mock(existing_obj: object | None) -> MagicMock:
    """Return a MagicMock session where query().filter_by().first() = existing_obj."""
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = existing_obj
    return session


def test_import_dataset_updates_existing_description():
    """When existing dataset found, description is updated from config."""
    existing = MagicMock()
    existing.id = 42

    session = _make_session_mock(existing)

    config = {
        "table_name": "sales",
        "schema": "public",
        "database_id": 1,
        "description": "New description from YAML",
        "columns": [],
        "metrics": [],
    }

    import superset.examples._ctx as _ctx_module
    from superset.examples.utils import _import_dataset

    with patch.object(_ctx_module, "session", session):
        result = _import_dataset(config)

    assert result is existing
    assert existing.description == "New description from YAML"


def test_import_dataset_updates_sql_on_existing():
    """When existing dataset found, sql is updated from config (stripped)."""
    existing = MagicMock()
    existing.id = 7

    session = _make_session_mock(existing)

    config = {
        "table_name": "vw",
        "schema": None,
        "database_id": 2,
        "sql": "  SELECT 1  ",
        "columns": [],
        "metrics": [],
    }

    import superset.examples._ctx as _ctx_module
    from superset.examples.utils import _import_dataset

    with patch.object(_ctx_module, "session", session):
        result = _import_dataset(config)

    assert result is existing
    # raw_sql should be stripped
    assert existing.sql == "SELECT 1"


def test_import_dataset_syncs_columns_on_existing():
    """When existing dataset found, columns are deleted and re-created from config."""
    existing = MagicMock()
    existing.id = 5

    session = _make_session_mock(existing)

    config = {
        "table_name": "tbl",
        "schema": None,
        "database_id": 3,
        "columns": [
            {"column_name": "id", "type": "INTEGER"},
            {"column_name": "name", "type": "VARCHAR(100)"},
        ],
        "metrics": [],
    }

    added_columns: list[str] = []

    import superset.examples._ctx as _ctx_module
    from superset.examples.utils import _import_dataset

    # Capture session.add calls to verify columns were re-created
    original_add = session.add

    def track_add(obj: object) -> None:
        from superset.models.connectors import TableColumn

        if isinstance(obj, TableColumn):
            added_columns.append(obj.column_name)
        original_add(obj)

    session.add = track_add

    with patch.object(_ctx_module, "session", session):
        result = _import_dataset(config)

    assert result is existing
    # Verify bulk delete was called (via query().filter_by().delete())
    session.query.return_value.filter_by.return_value.delete.assert_called()
    # Verify columns were re-inserted
    assert "id" in added_columns
    assert "name" in added_columns


def test_import_dataset_syncs_metrics_on_existing():
    """When existing dataset found, metrics are deleted and re-created from config."""
    existing = MagicMock()
    existing.id = 9

    session = _make_session_mock(existing)

    config = {
        "table_name": "facts",
        "schema": "dw",
        "database_id": 4,
        "columns": [],
        "metrics": [
            {"metric_name": "count", "expression": "COUNT(*)"},
            {"metric_name": "revenue", "expression": "SUM(amount)"},
        ],
    }

    added_metrics: list[str] = []

    import superset.examples._ctx as _ctx_module
    from superset.examples.utils import _import_dataset

    original_add = session.add

    def track_add(obj: object) -> None:
        from superset.models.connectors import SqlMetric

        if isinstance(obj, SqlMetric):
            added_metrics.append(obj.metric_name)
        original_add(obj)

    session.add = track_add

    with patch.object(_ctx_module, "session", session):
        result = _import_dataset(config)

    assert result is existing
    session.query.return_value.filter_by.return_value.delete.assert_called()
    assert "count" in added_metrics
    assert "revenue" in added_metrics


def test_import_dataset_no_existing_creates_new():
    """When no existing dataset, a new SqlaTable is created."""
    session = _make_session_mock(None)  # first() returns None

    config = {
        "table_name": "brand_new",
        "schema": "test",
        "database_id": 1,
        "columns": [],
        "metrics": [],
    }

    import superset.examples._ctx as _ctx_module
    from superset.examples.utils import _import_dataset

    with patch.object(_ctx_module, "session", session):
        _import_dataset(config)

    # A new SqlaTable should have been added to the session
    session.add.assert_called()
    from superset.models.connectors import SqlaTable

    added_obj = session.add.call_args[0][0]
    assert isinstance(added_obj, SqlaTable)
    assert added_obj.table_name == "brand_new"


def test_import_dataset_existing_force_data_calls_load():
    """When existing dataset and force_data=True, data loading is triggered."""
    existing = MagicMock()
    existing.id = 11

    session = _make_session_mock(existing)

    config = {
        "table_name": "sales",
        "schema": "pub",
        "database_id": 2,
        "data": "examples://sales.csv",
        "columns": [],
        "metrics": [],
    }

    import superset.examples._ctx as _ctx_module
    from superset.examples.utils import _import_dataset

    with (
        patch.object(_ctx_module, "session", session),
        patch("superset.examples.utils._load_dataset_data") as mock_load,
    ):
        result = _import_dataset(config, force_data=True)

    assert result is existing
    mock_load.assert_called_once()


def test_import_dataset_existing_no_force_data_skips_load():
    """When existing dataset and force_data=False, data loading is NOT triggered."""
    existing = MagicMock()
    existing.id = 12

    session = _make_session_mock(existing)

    config = {
        "table_name": "sales",
        "schema": "pub",
        "database_id": 2,
        "data": "examples://sales.csv",
        "columns": [],
        "metrics": [],
    }

    import superset.examples._ctx as _ctx_module
    from superset.examples.utils import _import_dataset

    with (
        patch.object(_ctx_module, "session", session),
        patch("superset.examples.utils._load_dataset_data") as mock_load,
    ):
        result = _import_dataset(config, force_data=False)

    assert result is existing
    mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# Finding 3: default_filters JSON errors must propagate (no silent swallow)
# Original: superset_old/commands/dashboard/importers/v1/utils.py lines 105-113
# has NO try/except; json.JSONDecodeError (ValueError subclass) propagates,
# aborting the dashboard import. The liteset code must NOT swallow these errors.
# ---------------------------------------------------------------------------


def test_default_filters_valid_json_remaps_ids():
    """Valid default_filters JSON: entries remapped; unmapped IDs dropped."""
    # id_map = {10: 100, 20: 200} — old IDs 10 and 20 are mapped; 30 is not
    import json as _json

    position = _make_position(
        [
            ("CHART-1", 10, "uuid-1"),
            ("CHART-2", 20, "uuid-2"),
        ]
    )
    chart_ids = {"uuid-1": 100, "uuid-2": 200}

    raw = {"10": {"val": "a"}, "20": {"val": "b"}, "30": {"val": "c"}}
    metadata = {"default_filters": _json.dumps(raw)}
    _call_update_metadata(metadata, position, chart_ids)

    result = _json.loads(metadata["default_filters"])
    # 10 -> 100, 20 -> 200; 30 is not in id_map and must be dropped
    assert result == {"100": {"val": "a"}, "200": {"val": "b"}}, (
        f"Unexpected default_filters result: {result}"
    )


def test_default_filters_malformed_json_raises_value_error():
    """Malformed default_filters JSON must propagate ValueError.

    Original (superset_old/commands/dashboard/importers/v1/utils.py:106)
    has no try/except around json.loads(metadata['default_filters']).
    The caller only catches KeyError, so a JSONDecodeError aborts the
    import. liteset must match that behaviour — no silent swallowing.
    """
    import json as _json

    import pytest

    position = _make_position([("CHART-1", 10, "uuid-1")])
    chart_ids = {"uuid-1": 100}

    metadata = {"default_filters": "NOT VALID JSON {{{"}

    with pytest.raises((ValueError, _json.JSONDecodeError)):
        _call_update_metadata(metadata, position, chart_ids)


def test_default_filters_non_string_raises_type_error():
    """Non-string default_filters must propagate TypeError.

    In the original json.loads(metadata['default_filters']) raises
    TypeError when the value is not a string/bytes — no swallowing.
    """
    import pytest

    position = _make_position([("CHART-1", 10, "uuid-1")])
    chart_ids = {"uuid-1": 100}

    # Passing a dict directly instead of a JSON string
    metadata = {"default_filters": {"10": {"val": "a"}}}

    with pytest.raises((TypeError, ValueError)):
        _call_update_metadata(metadata, position, chart_ids)


def test_default_filters_empty_object_json_gives_empty():
    """Empty default_filters JSON '{}' stays empty after remapping."""
    import json as _json

    position = _make_position([("CHART-1", 10, "uuid-1")])
    chart_ids = {"uuid-1": 100}

    metadata = {"default_filters": "{}"}
    _call_update_metadata(metadata, position, chart_ids)

    result = _json.loads(metadata["default_filters"])
    assert result == {}


# ---------------------------------------------------------------------------
# _import_dashboard — dedup by UUID (1:1 superset_old/commands/dashboard/
# importers/v1/utils.py:203), NOT by slug. Six example dashboards ship
# ``slug: null``; a slug-based lookup re-INSERTs them on every run.
# ---------------------------------------------------------------------------


def test_import_dashboard_dedupes_by_uuid_with_null_slug():
    """slug=None + known uuid → existing dashboard is UPDATED, not re-added."""
    import uuid as uuid_module

    existing = MagicMock()
    existing.id = 99

    session = _make_session_mock(existing)
    dash_uuid = "11111111-2222-3333-4444-555555555555"

    config = {
        "uuid": dash_uuid,
        "slug": None,
        "dashboard_title": "COVID Vaccine Dashboard",
        "position": {},
        "metadata": {},
    }

    import superset.examples._ctx as _ctx_module
    from superset.examples.utils import _import_dashboard

    with patch.object(_ctx_module, "session", session):
        result = _import_dashboard(config, chart_ids={}, dataset_info={})

    assert result is existing
    session.add.assert_not_called()
    session.query.return_value.filter_by.assert_called_once_with(
        uuid=uuid_module.UUID(dash_uuid)
    )
    assert existing.dashboard_title == "COVID Vaccine Dashboard"


def test_import_dashboard_creates_new_when_uuid_unknown():
    """Unknown uuid → a new Dashboard is added with that uuid."""
    import uuid as uuid_module

    session = _make_session_mock(None)
    dash_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    config = {
        "uuid": dash_uuid,
        "slug": None,
        "dashboard_title": "Fresh Dashboard",
        "position": {},
        "metadata": {},
    }

    import superset.examples._ctx as _ctx_module
    from superset.examples.utils import _import_dashboard

    with patch.object(_ctx_module, "session", session):
        result = _import_dashboard(config, chart_ids={}, dataset_info={})

    session.add.assert_called_once_with(result)
    assert result.uuid == uuid_module.UUID(dash_uuid)
