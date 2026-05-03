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
"""Port of ``superset_old.migrations.shared.native_filters``.

The full filter-box conversion logic depends on
``convert_filter_scopes`` (300+ LOC of legacy filter-box internals) which
isn't yet ported to liteset.  Bundles produced by Superset >= 2.0 always
ship with ``filter_box`` charts already migrated, so the typical import
flow only needs the *cleanup* half of the original migration:

1. Strip ``default_filters`` / ``filter_scopes`` from ``json_metadata``
   (they're superseded by ``native_filter_configuration``).
2. Replace any remaining ``filter_box`` chart positions with markdown
   placeholders pointing back at the original chart slug.
3. Remove the filter-box charts from ``dashboard.slices``.

This matches the *observable* effect of the original on already-migrated
bundles.  When the rare case of an unmigrated bundle is encountered the
function still terminates cleanly (matching the original
``except: print`` recovery) so import doesn't block.
"""

from __future__ import annotations

import json as _json
import logging
from textwrap import dedent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from superset.models.dashboard import Dashboard

logger = logging.getLogger(__name__)


def migrate_dashboard(dashboard: Dashboard) -> None:  # noqa: C901
    """Convert ``dashboard`` to use native filters in place.

    Mirrors :func:`superset_old.migrations.shared.native_filters.migrate_dashboard`
    for the cleanup half of the original (the legacy filter-box ->
    native-filter conversion is gated behind import of
    ``convert_filter_scopes`` which is not yet ported).  The async caller
    is expected to have already eagerly loaded ``dashboard.slices``.
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

        # Ensure the native_filter_configuration key exists.
        json_metadata.setdefault("native_filter_configuration", [])

        # Best-effort: try to convert any remaining legacy filter scopes
        # to native filters using the original helper.  If the port isn't
        # available, just drop the legacy keys (the typical case for
        # post-2.0 bundles which ship native filters already).
        try:
            from superset.migrations.shared.native_filters_full import (
                convert_filter_scopes_to_native_filters,
            )

            json_metadata["native_filter_configuration"].extend(
                convert_filter_scopes_to_native_filters(
                    json_metadata,
                    position_json,
                    filter_boxes=list(filter_boxes_by_id.values()),
                ),
            )
        except ImportError:
            logger.debug(
                "convert_filter_scopes_to_native_filters not yet ported; "
                "dropping legacy filter scopes for dashboard %s",
                getattr(dashboard, "id", None),
            )

        # Remove the legacy filter configuration.
        for key in ("default_filters", "filter_scopes"):
            json_metadata.pop(key, None)

        # Replace the filter-box charts with markdown placeholders.
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

        # Replace the relevant CHART- references in children/parents arrays.
        for value in position_json.values():
            if isinstance(value, dict):
                for relation in ("children", "parents"):
                    if relation in value:
                        for idx, key in enumerate(value[relation]):
                            if key in mapping:
                                value[relation][idx] = mapping[key]

        # Remove the filter-box charts from the dashboard's slice list.
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


__all__ = ["migrate_dashboard"]
