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
"""Verify all models are importable from superset.models."""
from __future__ import annotations

import importlib

import pytest


# All model modules that must exist in superset/models/
MODEL_MODULES = [
    "superset.models",
    "superset.models.core",
    "superset.models.sql_lab",
    "superset.models.tags",
    "superset.models.reports",
    "superset.models.annotations",
    "superset.models.helpers",
    "superset.models.embedded_dashboard",
    "superset.models.css_template",
    "superset.models.user",
    "superset.models.connectors",
    "superset.models.cache",
    "superset.models.dynamic_plugins",
    "superset.models.key_value",
]


@pytest.mark.parametrize("module_name", MODEL_MODULES)
def test_model_module_importable(module_name: str):
    """Each model module must be importable."""
    mod = importlib.import_module(module_name)
    assert mod is not None


def test_core_models_importable():
    """Key model classes must be importable from superset.models.core."""
    from superset.models.core import Database, CssTemplate, Theme, Log, FavStar

    assert Database is not None
    assert CssTemplate is not None
    assert Theme is not None
    assert Log is not None
    assert FavStar is not None


def test_dashboard_models_importable():
    """Dashboard model and association tables must be importable."""
    from superset.models.dashboard import Dashboard, dashboard_slices

    assert Dashboard is not None
    assert dashboard_slices is not None


def test_slice_model_importable():
    """Slice model must be importable."""
    from superset.models.slice import Slice

    assert Slice is not None


def test_sqllab_models_importable():
    """SQLLab model classes must be importable."""
    from superset.models.sql_lab import Query, SavedQuery, TabState

    assert Query is not None
    assert SavedQuery is not None
    assert TabState is not None


def test_connector_models_importable():
    """Connector models must be importable."""
    from superset.models.connectors import SqlaTable, TableColumn, SqlMetric

    assert SqlaTable is not None
    assert TableColumn is not None
    assert SqlMetric is not None


def test_report_models_importable():
    """Report models and enums must be importable."""
    from superset.models.reports import ReportSchedule, ReportExecutionLog, ReportState

    assert ReportSchedule is not None
    assert ReportExecutionLog is not None
    assert ReportState is not None


def test_helpers_importable():
    """Mixin classes must be importable."""
    from superset.models.helpers import AuditMixinNullable, Base

    assert AuditMixinNullable is not None
    assert Base is not None


def test_no_flask_imports_in_daos():
    """Verify no DAO file imports from Flask or Flask-AppBuilder."""
    import ast
    from pathlib import Path

    dao_dir = Path("superset/db/daos")
    if not dao_dir.exists():
        pytest.skip("DAO directory not found")

    violations: list[str] = []
    for py_file in dao_dir.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(
                    ("flask", "flask_appbuilder")
                ):
                    violations.append(
                        f"{py_file.name}: imports from "
                        f"{node.module}"
                    )

    assert not violations, f"Flask imports in DAOs: {violations}"
