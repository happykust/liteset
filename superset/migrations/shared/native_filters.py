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
"""Legacy filter-box → native-filter migration utilities.

Includes the full legacy filter-box → native-filter conversion
(:func:`convert_filter_scopes_to_native_filters`;
``convert_filter_scopes`` lives in
``superset.utils.dashboard_filter_scopes_converter``) plus the cleanup half
of ``migrate_dashboard`` (strip legacy keys, markdown placeholders, drop
filter-box charts from ``dashboard.slices``).
"""

from __future__ import annotations

import json as _json
import logging
from collections import defaultdict
from textwrap import dedent
from typing import Any, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

logger = logging.getLogger(__name__)


def convert_filter_scopes_to_native_filters(  # noqa: C901, PLR0912, PLR0915
    json_metadata: dict[str, Any],
    position_json: dict[str, Any],
    filter_boxes: list[Slice],
) -> list[dict[str, Any]]:
    """
    Convert the legacy filter scopes et al. to the native filter configuration.

    Dashboard filter scopes are implicitly defined where an undefined scope
    implies no immunity; ``convert_filter_scopes`` provides an explicit
    definition by extracting the underlying filter-box configurations.

    Hierarchical legacy filters are defined via non-exclusion of peer or
    children filter-box charts whereas native hierarchical filters are
    defined via explicit parental relationships, i.e., the inverse.
    """
    from superset.utils import json
    from superset.utils.core import shortid
    from superset.utils.dashboard_filter_scopes_converter import (
        convert_filter_scopes,
    )

    default_filters = json.loads(json_metadata.get("default_filters") or "{}")
    filter_scopes = json_metadata.get("filter_scopes", {})
    filter_box_ids = {filter_box.id for filter_box in filter_boxes}

    filter_scope_by_key_and_field: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        dict
    )

    filter_by_key_and_field: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    # Dense representation of filter scopes, falling back to chart level filter
    # configs if the respective filter scope is not defined at the dashboard
    # level.
    for filter_box in filter_boxes:
        key = str(filter_box.id)

        filter_scope_by_key_and_field[key] = {
            **(
                convert_filter_scopes(
                    json_metadata,
                    filter_boxes=[filter_box],
                ).get(cast("int", filter_box.id), {})
            ),
            **(filter_scopes.get(key, {})),
        }

    unique_short_ids: set[str] = set()
    for filter_box in filter_boxes:
        key = str(filter_box.id)
        params = json.loads(cast("str", filter_box.params) or "{}")

        for field, filter_scope in filter_scope_by_key_and_field[key].items():
            default = default_filters.get(key, {}).get(field)
            short_id = f"{shortid()}"[:9]

            # Ensure uniqueness due to UUIDv4 truncation increasing
            # collision chance to infinitesimally small amount.
            while True:
                if short_id not in unique_short_ids:
                    unique_short_ids.add(short_id)
                    break
                short_id = f"{shortid()}"[:9]

            fltr: dict[str, Any] = {
                "cascadeParentIds": [],
                "id": f"NATIVE_FILTER-{short_id}",
                "scope": {
                    "rootPath": filter_scope["scope"],
                    "excluded": [
                        id_
                        for id_ in filter_scope["immune"]
                        if id_ not in filter_box_ids
                    ],
                },
                "type": "NATIVE_FILTER",
            }

            if field == "__time_col" and params.get("show_sqla_time_column"):
                fltr.update(
                    {
                        "filterType": "filter_timecolumn",
                        "name": "Time Column",
                        "targets": [{"datasetId": filter_box.datasource_id}],
                    }
                )

                if not default:
                    default = params.get("granularity_sqla")

                if default:
                    fltr["defaultDataMask"] = {
                        "extraFormData": {"granularity_sqla": default},
                        "filterState": {"value": [default]},
                    }
            elif field == "__time_grain" and params.get("show_sqla_time_granularity"):
                fltr.update(
                    {
                        "filterType": "filter_timegrain",
                        "name": "Time Grain",
                        "targets": [{"datasetId": filter_box.datasource_id}],
                    }
                )

                if not default:
                    default = params.get("time_grain_sqla")

                if default:
                    fltr["defaultDataMask"] = {
                        "extraFormData": {"time_grain_sqla": default},
                        "filterState": {"value": [default]},
                    }
            elif field == "__time_range" and params.get("date_filter"):
                fltr.update(
                    {
                        "filterType": "filter_time",
                        "name": "Time Range",
                        "targets": [{}],
                    }
                )

                if not default:
                    default = params.get("time_range")

                if default and default != "No filter":
                    fltr["defaultDataMask"] = {
                        "extraFormData": {"time_range": default},
                        "filterState": {"value": default},
                    }
            else:
                for config in params.get("filter_configs") or []:
                    if config["column"] == field:
                        fltr.update(
                            {
                                "controlValues": {
                                    "defaultToFirstItem": False,
                                    "enableEmptyFilter": not config.get(
                                        "clearable",
                                        True,
                                    ),
                                    "inverseSelection": False,
                                    "multiSelect": config.get(
                                        "multiple",
                                        False,
                                    ),
                                    "searchAllOptions": config.get(
                                        "searchAllOptions",
                                        False,
                                    ),
                                },
                                "filterType": "filter_select",
                                "name": config.get("label") or field,
                                "targets": [
                                    {
                                        "column": {"name": field},
                                        "datasetId": filter_box.datasource_id,
                                    },
                                ],
                            }
                        )

                        if "metric" in config:
                            fltr["sortMetric"] = config["metric"]
                            fltr["controlValues"]["sortAscending"] = config["asc"]

                        if params.get("adhoc_filters"):
                            fltr["adhoc_filters"] = params["adhoc_filters"]

                        # Pre-filter available values based on time range/column.
                        time_range = params.get("time_range")

                        if time_range and time_range != "No filter":
                            fltr.update(
                                {
                                    "time_range": time_range,
                                    "granularity_sqla": params.get("granularity_sqla"),
                                }
                            )

                        if not default:
                            default = config.get("defaultValue")

                            if default and config["multiple"]:
                                default = default.split(";")

                        if default:
                            if not isinstance(default, list):
                                default = [default]

                            fltr["defaultDataMask"] = {
                                "extraFormData": {
                                    "filters": [
                                        {
                                            "col": field,
                                            "op": "IN",
                                            "val": default,
                                        }
                                    ],
                                },
                                "filterState": {"value": default},
                            }

                        break

            if "filterType" in fltr:
                filter_by_key_and_field[key][field] = fltr

    ancestors_by_id = defaultdict(set)

    for filter_box in filter_boxes:
        for value in position_json.values():
            try:
                if (
                    isinstance(value, dict)
                    and value["type"] == "CHART"
                    and value["meta"]["chartId"] == filter_box.id
                    and value["parents"]  # Misnomer as this the complete ancestry.
                ):
                    ancestors_by_id[filter_box.id] = set(value["parents"])
            except KeyError:
                pass

    for this in filter_boxes:
        for other in filter_boxes:
            if this != other and any(
                # Immunity is at the chart rather than field level.
                this.id not in filter_scope["immune"]
                and set(filter_scope["scope"]) <= ancestors_by_id[this.id]
                for filter_scope in filter_scope_by_key_and_field[
                    str(other.id)
                ].values()
            ):
                for child in filter_by_key_and_field[str(this.id)].values():
                    if child["filterType"] == "filter_select":
                        for parent in filter_by_key_and_field[str(other.id)].values():
                            if (
                                parent["filterType"] in {"filter_select", "filter_time"}
                                and parent["id"] not in child["cascadeParentIds"]
                            ):
                                child["cascadeParentIds"].append(parent["id"])

    return sorted(
        [
            fltr
            for key in filter_by_key_and_field
            for fltr in filter_by_key_and_field[key].values()
        ],
        key=lambda fltr: fltr["filterType"],
    )


def migrate_dashboard(dashboard: Dashboard) -> None:  # noqa: C901
    """Convert ``dashboard`` to use native filters in place.

    The async caller is expected to have already eagerly loaded
    ``dashboard.slices``.
    """
    mapping: dict[str, str] = {}

    try:
        json_metadata = _json.loads(str(dashboard.json_metadata or "{}"))
        position_json = _json.loads(str(dashboard.position_json or "{}"))

        slices = list(getattr(dashboard, "slices", None) or [])

        filter_boxes_by_id = {
            slc.id: slc
            for slc in slices
            if getattr(slc, "viz_type", None) == "filter_box"
        }

        json_metadata.setdefault("native_filter_configuration", [])

        # Convert any remaining legacy filter scopes to native filters —
        # (The previous version imported a phantom
        # ``native_filters_full`` module whose ImportError ALWAYS fired,
        # silently dropping legacy filter-box scopes on import.)
        json_metadata["native_filter_configuration"].extend(
            convert_filter_scopes_to_native_filters(
                json_metadata,
                position_json,
                filter_boxes=list(filter_boxes_by_id.values()),
            ),
        )

        for key in ("default_filters", "filter_scopes"):
            json_metadata.pop(key, None)

        for key, value in list(position_json.items()):  # mutable iteration
            if (
                isinstance(value, dict)
                and value.get("type") == "CHART"
                and (meta := value.get("meta"))
                and meta.get("chartId") in filter_boxes_by_id
            ):
                slc = filter_boxes_by_id[meta["chartId"]]
                mapping[key] = key.replace("CHART-", "MARKDOWN-")

                value["id"] = mapping[key]
                value["type"] = "MARKDOWN"
                meta["code"] = dedent(
                    f"""
                        &#9888; The <a href="/superset/slice/{slc.id}/">{slc.slice_name}
                        </a> filter-box chart has been migrated to a native filter.
                        """
                )

                position_json[mapping[key]] = value
                del position_json[key]

        for value in position_json.values():
            if isinstance(value, dict):
                for relation in ("children", "parents"):
                    if relation in value:
                        for idx, key in enumerate(value[relation]):
                            if key in mapping:
                                value[relation][idx] = mapping[key]

        if filter_boxes_by_id:
            dashboard.slices = [
                slc for slc in slices if getattr(slc, "viz_type", None) != "filter_box"
            ]

        # Use ``setattr`` to bypass the SA Column descriptor type check —
        # the underlying mapped attribute accepts a plain ``str`` at runtime.
        setattr(dashboard, "json_metadata", _json.dumps(json_metadata))  # noqa: B010
        setattr(dashboard, "position_json", _json.dumps(position_json))  # noqa: B010
    except Exception:  # noqa: BLE001
        logger.exception(
            "Unable to migrate dashboard %s to native filters",
            getattr(dashboard, "id", None),
        )


__all__ = ["convert_filter_scopes_to_native_filters", "migrate_dashboard"]
