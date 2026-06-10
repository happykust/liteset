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
"""/related/{column} ``text`` resolution — model ``__repr__`` + per-column
``text_field_rel_fields`` override.

The original resolves dropdown text via ``_get_text_for_model``
(superset_old/views/base_api.py:403-408): ``text_field_rel_fields`` first,
``str(model)`` otherwise. ``str(model)`` only produces human-readable labels
because the FAB/Superset models define ``__repr__`` — liteset models must do
the same or every dropdown shows ``<...Dashboard object at 0x...>``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Model __repr__ — 1:1 with the originals
# ---------------------------------------------------------------------------


def test_slice_repr_is_slice_name():
    """superset_old/models/slice.py:140 — ``slice_name or str(id)``."""
    from superset.models.slice import Slice

    slc = Slice()
    slc.slice_name = "Top Charts"
    assert str(slc) == "Top Charts"
    slc.slice_name = None
    slc.id = 17
    assert str(slc) == "17"


def test_dashboard_repr():
    """superset_old/models/dashboard.py:184 — ``Dashboard<{id or slug}>``."""
    from superset.models.dashboard import Dashboard

    dash = Dashboard()
    dash.id = 5
    assert str(dash) == "Dashboard<5>"
    dash.id = None
    dash.slug = "world-health"
    assert str(dash) == "Dashboard<world-health>"


def test_saved_query_repr_is_label():
    """superset_old/models/sql_lab.py:439 — ``str(label)``."""
    from superset.models.sql_lab import SavedQuery

    sq = SavedQuery()
    sq.label = "Daily revenue"
    assert str(sq) == "Daily revenue"


def test_role_repr_is_name():
    """FAB Role.__repr__ (flask_appbuilder/security/sqla/models.py:132)."""
    from superset.models.security import Role

    role = Role()
    role.name = "Gamma"
    assert str(role) == "Gamma"


def test_sqla_table_repr_is_name():
    """superset_old/connectors/sqla/models.py:1186 — ``self.name``."""
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    tbl.table_name = "wb_health_population"
    assert "wb_health_population" in str(tbl)


def test_annotation_layer_repr_is_name():
    """superset_old/models/annotations.py:37 — ``str(self.name)``."""
    from superset.models.annotations import AnnotationLayer

    layer = AnnotationLayer()
    layer.name = "Holidays"
    assert str(layer) == "Holidays"


# ---------------------------------------------------------------------------
# Reports controller passes text_field_rel_fields (superset_old/reports/
# api.py:233-237) through to get_related_payload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_related_passes_text_field_rel_fields():
    from superset.controllers.report import ReportScheduleController

    handler = ReportScheduleController.related.fn

    captured: dict = {}

    async def _fake_related_payload(**kwargs):
        captured.update(kwargs)
        return {"count": 0, "result": []}

    with (
        patch(
            "superset.controllers.report.get_related_payload",
            new=_fake_related_payload,
        ),
        patch(
            "superset.db.filters.report_access_filters",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await handler(
            None,
            column_name="dashboard",
            dao=MagicMock(),
            rison_params=None,
            current_user=MagicMock(),
            security_manager=MagicMock(),
        )

    assert captured.get("text_field_rel_fields") == {
        "dashboard": "dashboard_title",
        "chart": "slice_name",
        "database": "database_name",
    }
