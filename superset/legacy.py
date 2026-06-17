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
"""Compatibility shim for ``superset.legacy``.

Legacy migrations import:
  - ``update_time_range``
"""

from __future__ import annotations

from typing import Any


def update_time_range(form_data: dict[str, Any]) -> None:
    """Legacy adjustments to time range.

    - Move ``since`` and ``until`` to ``time_range``.
    - Define ``time_range`` when ``granularity_sqla`` is set and unfiltered.
    """
    if "since" in form_data or "until" in form_data:
        since = form_data.pop("since", "") or ""
        until = form_data.pop("until", "") or ""
        form_data["time_range"] = f"{since} : {until}"

    if temporal_column := form_data.get("granularity_sqla"):
        if any(
            adhoc_filter.get("subject") == temporal_column
            and adhoc_filter.get("comparator") == "No filter"
            for adhoc_filter in form_data.get("adhoc_filters", [])
        ):
            form_data.setdefault("time_range", "No filter")


__all__ = ["update_time_range"]


CONTAINER_TYPES = ["COLUMN", "GRID", "TABS", "TAB", "ROW"]


def is_slice_in_container(
    layout: dict[str, dict[str, Any]], container_id: str, slice_id: int
) -> bool:
    if container_id == "ROOT_ID":
        return True

    node = layout[container_id]
    node_type = node.get("type")
    if node_type == "CHART" and node.get("meta", {}).get("chartId") == slice_id:
        return True

    if node_type in CONTAINER_TYPES:
        children = node.get("children", [])
        return any(
            is_slice_in_container(layout, child_id, slice_id) for child_id in children
        )

    return False


def build_extra_filters(  # noqa: C901
    layout: dict[str, dict[str, Any]],
    filter_scopes: dict[str, dict[str, Any]],
    default_filters: dict[str, dict[str, list[Any]]],
    slice_id: int,
    filter_params_by_id: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """The original reads each filter-box Slice's ``params`` off the sync
    ``db.session`` inline; in the async port the caller pre-fetches them and
    passes ``filter_params_by_id`` (keyed by the stringified slice id) so
    this stays a pure function.
    """
    import json as _json

    extra_filters = []
    filter_params_by_id = filter_params_by_id or {}

    # do not apply filters if chart is not in filter's scope or chart is
    # immune to the filter.
    for filter_id, columns in default_filters.items():
        raw_params = filter_params_by_id.get(str(filter_id))

        filter_configs: list[dict[str, Any]] = []
        if raw_params is not None:
            filter_configs = _json.loads(raw_params or "{}").get("filter_configs") or []

        scopes_by_filter_field = filter_scopes.get(filter_id, {})
        for col, val in columns.items():
            if not val:
                continue

            current_field_scopes = scopes_by_filter_field.get(col, {})
            scoped_container_ids = current_field_scopes.get("scope", ["ROOT_ID"])
            immune_slice_ids = current_field_scopes.get("immune", [])

            for container_id in scoped_container_ids:
                if slice_id not in immune_slice_ids and is_slice_in_container(
                    layout, container_id, slice_id
                ):
                    # Ensure that the filter value encoding adheres to the
                    # filter select type.
                    for filter_config in filter_configs:
                        if filter_config["column"] == col:
                            is_multiple = filter_config["multiple"]

                            if not is_multiple and isinstance(val, list):
                                val = val[0]
                            elif is_multiple and not isinstance(val, list):
                                val = [val]
                            break

                    extra_filters.append(
                        {
                            "col": col,
                            "op": "in" if isinstance(val, list) else "==",
                            "val": val,
                        }
                    )

    return extra_filters
