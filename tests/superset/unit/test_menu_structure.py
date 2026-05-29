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
"""Menu-structure parity with the original ``initialization/__init__.py``.

The menu tree (``superset.controllers.menu._build_menu_tree``) is a hand-built
mirror of FAB's ``add_view`` / ``add_link`` registration order.  These tests
pin the order- and icon-sensitive bits that drift easily.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from superset.controllers.menu import _filter_menu_for_user


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        feature_flags={
            "DYNAMIC_PLUGINS": True,
            "CSS_TEMPLATES": True,
            "TAGGING_SYSTEM": True,
            "ALERT_REPORTS": True,
        },
        logo_target_path=None,
        superset_security_view_menu=True,
        auth_user_registration=False,
        fab_add_security_views=True,
        superset_log_view=True,
        auth_role_admin="Admin",
    )


def _admin() -> SimpleNamespace:
    return SimpleNamespace(roles=[SimpleNamespace(name="Admin")], permissions=set())


def _by_name(menu: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(
        item for item in menu if isinstance(item, dict) and item["name"] == name
    )


def _child_names(category: dict[str, Any]) -> list[str]:
    return [c["name"] for c in category["childs"]]


def test_security_action_log_before_row_level_security() -> None:
    """Action Log is registered into Security before RLS (init order)."""
    menu = _filter_menu_for_user(_admin(), _settings())
    names = _child_names(_by_name(menu, "Security"))
    assert names.index("Action Log") < names.index("Row Level Security")


def test_data_category_has_no_icon() -> None:
    """The Data *category* carries no icon (only the Databases child does)."""
    menu = _filter_menu_for_user(_admin(), _settings())
    data = _by_name(menu, "Data")
    assert data["icon"] == ""
    databases = _by_name(data["childs"], "Databases")
    assert databases["icon"] == "fa-database"


def test_top_level_order() -> None:
    """Top-level categories appear in first-reference (registration) order."""
    menu = _filter_menu_for_user(_admin(), _settings())
    names = [item["name"] for item in menu if isinstance(item, dict)]
    # Home is hidden (LOGO_TARGET_PATH unset).
    assert names == [
        "Data",
        "Dashboards",
        "Charts",
        "Datasets",
        "Security",
        "Manage",
        "SQL Lab",
    ]


def test_manage_order() -> None:
    """Manage children keep init-registration order."""
    menu = _filter_menu_for_user(_admin(), _settings())
    names = _child_names(_by_name(menu, "Manage"))
    assert names == [
        "Plugins",
        "CSS Templates",
        "Themes",
        "Tags",
        "Alerts & Report",
        "Annotation Layers",
    ]
