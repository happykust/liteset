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
"""Menu API controller.

Port of Flask-AppBuilder's ``MenuApi`` (``flask_appbuilder/menu.py``).

The original endpoint ``GET /api/v1/menu/`` returns a forest-like menu
structure filtered by the current user's ``menu_access`` permissions.
This Litestar controller reproduces that behaviour:

1. A static menu tree mirrors the items registered via
   ``appbuilder.add_view`` / ``appbuilder.add_link`` in the original
   ``superset/initialization/__init__.py``.
2. Each item may carry a ``cond`` callable that mirrors FAB's
   ``MenuItem.should_render()`` — if it returns falsy the item is hidden.
3. The tree is walked *recursively* exactly like FAB's ``Menu.get_data()``:
   - ``should_render()`` / ``cond`` is evaluated first.
   - Separator items (``name == "-"``) are passed through.
   - Items whose ``name`` is not in the user's allowed-menus set are skipped.
   - Category items recurse into ``childs``; leaf items emit ``url``.
4. Labels are wrapped with ``gettext()`` for i18n, matching FAB's
   ``__(str(item.label))``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from litestar import Controller, get, Request
from litestar.datastructures import State

from superset.guards.rbac import require_permission
from superset.i18n import gettext as _

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MenuItem: lightweight mirror of ``flask_appbuilder.menu.MenuItem``
# ---------------------------------------------------------------------------
class MenuItem:
    """Static menu item, matching FAB's ``MenuItem`` API."""

    __slots__ = ("name", "href", "icon", "label", "childs", "cond")

    def __init__(
        self,
        name: str,
        href: str = "",
        icon: str = "",
        label: str = "",
        childs: list["MenuItem"] | None = None,
        cond: Callable[[], bool] | None = None,
    ) -> None:
        self.name = name
        self.href = href
        self.icon = icon
        self.label = label or name
        self.childs: list[MenuItem] = childs if childs is not None else []
        self.cond = cond

    def should_render(self) -> bool:
        """Evaluate the condition lambda, matching FAB's logic."""
        return bool(self.cond()) if self.cond is not None else True


# ---------------------------------------------------------------------------
# Menu tree builder — called once per request with the resolved settings
# to build the static tree with condition lambdas bound to settings values.
# ---------------------------------------------------------------------------
def _build_menu_tree(settings: Any) -> list[MenuItem]:
    """Build the full menu tree mirroring ``superset/initialization/__init__.py``.

    Each item carries its ``cond`` lambda exactly as registered in the
    original Superset init code.  The tree structure matches the order
    in which ``appbuilder.add_view`` / ``appbuilder.add_link`` are called.
    """
    feature_flags: dict[str, bool] = getattr(settings, "feature_flags", {})

    def _ff(flag: str) -> bool:
        return bool(feature_flags.get(flag, False))

    logo_target_path: str | None = getattr(settings, "logo_target_path", None)
    superset_security_view_menu: bool = getattr(
        settings, "superset_security_view_menu", True
    )
    auth_user_registration: bool = getattr(settings, "auth_user_registration", False)
    fab_add_security_views: bool = getattr(settings, "fab_add_security_views", True)
    superset_log_view: bool = getattr(settings, "superset_log_view", True)

    return [
        # -- Home (conditional on LOGO_TARGET_PATH) --
        MenuItem(
            name="Home",
            href="/superset/welcome/",
            label="Home",
            cond=lambda: bool(logo_target_path),
        ),
        # -- Data category --
        # The Data *category* has no icon — upstream's
        # ``add_view(DatabaseView, ..., category="Data")`` sets only the
        # ``icon`` on the Databases item, not ``category_icon`` (which
        # defaults to ""). The ``fa-database`` icon belongs on the child.
        MenuItem(
            name="Data",
            label="Data",
            childs=[
                MenuItem(
                    name="Databases",
                    href="/databaseview/list/",
                    icon="fa-database",
                    label="Database Connections",
                ),
            ],
        ),
        # -- Dashboards (top-level, no category) --
        MenuItem(
            name="Dashboards",
            href="/dashboard/list/",
            icon="fa-dashboard",
            label="Dashboards",
        ),
        # -- Charts (top-level, no category) --
        MenuItem(
            name="Charts",
            href="/chart/list/",
            icon="fa-bar-chart",
            label="Charts",
        ),
        # -- Datasets (top-level, no category) --
        MenuItem(
            name="Datasets",
            href="/tablemodelview/list/",
            icon="fa-table",
            label="Datasets",
        ),
        # -- Security category --
        MenuItem(
            name="Security",
            label="Security",
            childs=[
                MenuItem(
                    name="List Roles",
                    href="/roles/",
                    label="List Roles",
                    cond=lambda: bool(superset_security_view_menu),
                ),
                MenuItem(
                    name="User Registrations",
                    href="/registrations/",
                    label="User Registrations",
                    cond=lambda: bool(auth_user_registration),
                ),
                MenuItem(
                    name="List Users",
                    href="/users/",
                    label="List Users",
                    cond=lambda: bool(superset_security_view_menu),
                ),
                MenuItem(
                    name="List Groups",
                    href="/list_groups/",
                    label="List Groups",
                    cond=lambda: bool(superset_security_view_menu),
                ),
                # Registration order in the original
                # ``initialization/__init__.py``: Action Log (line 455) is
                # added to the Security category BEFORE Row Level Security
                # (line 493), so it renders first.
                MenuItem(
                    name="Action Log",
                    href="/actionlog/list",
                    icon="fa-list-ol",
                    label="Action Log",
                    cond=lambda: bool(fab_add_security_views and superset_log_view),
                ),
                MenuItem(
                    name="Row Level Security",
                    href="/rowlevelsecurity/list/",
                    icon="fa-lock",
                    label="Row Level Security",
                ),
            ],
        ),
        # -- Manage category --
        MenuItem(
            name="Manage",
            label="Manage",
            childs=[
                MenuItem(
                    name="Plugins",
                    href="/plugins/",
                    icon="fa-puzzle-piece",
                    label="Plugins",
                    cond=lambda: _ff("DYNAMIC_PLUGINS"),
                ),
                MenuItem(
                    name="CSS Templates",
                    href="/csstemplatemodelview/list/",
                    icon="fa-css3",
                    label="CSS Templates",
                    cond=lambda: _ff("CSS_TEMPLATES"),
                ),
                MenuItem(
                    name="Themes",
                    href="/theme/list/",
                    icon="fa-palette",
                    label="Themes",
                ),
                MenuItem(
                    name="Tags",
                    href="/superset/tags/",
                    label="Tags",
                    cond=lambda: _ff("TAGGING_SYSTEM"),
                ),
                MenuItem(
                    name="Alerts & Report",
                    href="/alert/list/",
                    icon="fa-exclamation-triangle",
                    label="Alerts & Reports",
                    cond=lambda: _ff("ALERT_REPORTS"),
                ),
                MenuItem(
                    name="Annotation Layers",
                    href="/annotationlayer/list/",
                    icon="fa-comment",
                    label="Annotation Layers",
                ),
            ],
        ),
        # -- SQL Lab category --
        MenuItem(
            name="SQL Lab",
            icon="fa-flask",
            label="SQL",
            childs=[
                MenuItem(
                    name="SQL Editor",
                    href="/sqllab/",
                    icon="fa-flask",
                    label="SQL Lab",
                ),
                MenuItem(
                    name="Saved Queries",
                    href="/savedqueryview/list/",
                    icon="fa-save",
                    label="Saved Queries",
                ),
                MenuItem(
                    name="Query Search",
                    href="/sqllab/history/",
                    icon="fa-search",
                    label="Query History",
                ),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# Recursive get_data — mirrors FAB's ``Menu.get_data()`` exactly.
# ---------------------------------------------------------------------------
def _get_flat_name_list(menu: list[MenuItem]) -> list[str]:
    """Collect all menu item names recursively (for permission lookup).

    Mirrors FAB's ``Menu.get_flat_name_list()``.
    """
    result: list[str] = []
    for item in menu:
        result.append(item.name)
        if item.childs:
            result.extend(_get_flat_name_list(item.childs))
    return result


def _get_data(
    menu: list[MenuItem],
    allowed_menus: set[str],
) -> list[dict[str, Any]]:
    """Walk the menu tree and produce the JSON response.

    This is a direct port of FAB's ``Menu.get_data()`` (lines 65-99 of
    ``flask_appbuilder/menu.py``):

    - ``should_render()`` is checked first (evaluates ``cond`` lambdas).
    - Separator items (``name == "-"``) are passed through.
    - Items whose ``name`` is not in ``allowed_menus`` are skipped.
    - Category items (those with ``childs``) recurse; their ``childs``
      key holds the filtered children.
    - Leaf items emit ``url`` (no ``childs`` key).
    - Categories are included even when ``childs`` is empty after
      filtering, matching FAB's ``get_data()`` behavior.
    - Labels are wrapped with ``gettext`` for i18n.
    """
    ret_list: list[dict[str, Any]] = []

    for i, item in enumerate(menu):
        if not item.should_render():
            continue

        # Separator
        if item.name == "-" and i != len(menu) - 1:
            ret_list.append("-")  # type: ignore[arg-type]
            continue

        # Permission check
        if item.name not in allowed_menus:
            continue

        if item.childs:
            # Category item — recurse into children.
            # FAB's get_data() includes categories even when childs is empty
            # after filtering, so we do NOT skip empty categories.
            filtered_childs = _get_data(item.childs, allowed_menus)
            ret_list.append(
                {
                    "name": item.name,
                    "icon": item.icon,
                    "label": _(str(item.label)),
                    "childs": filtered_childs,
                }
            )
        else:
            # Leaf item
            ret_list.append(
                {
                    "name": item.name,
                    "icon": item.icon,
                    "label": _(str(item.label)),
                    "url": item.href,
                }
            )

    return ret_list


# ---------------------------------------------------------------------------
# Public filter function (also usable by spa.py)
# ---------------------------------------------------------------------------
def _filter_menu_for_user(
    user: Any,
    settings: Any,
) -> list[dict[str, Any]]:
    """Filter the menu tree by user permissions and conditions.

    Reproduces the full filtering pipeline from FAB's ``Menu.get_data()``:

    1. Build the static menu tree with condition lambdas.
    2. Collect all item names (flat) for the allowed-menus set.
    3. Determine the user's allowed menus from their permissions.
       Admin users see *all* menu names.
    4. Walk the tree recursively, applying ``should_render()``,
       permission checks, and child filtering.
    """
    menu_tree = _build_menu_tree(settings)

    # Collect every menu name in the tree
    all_names = _get_flat_name_list(menu_tree)

    # Determine if user is admin
    is_admin = False
    user_roles = getattr(user, "roles", [])
    admin_role_name = getattr(settings, "auth_role_admin", "Admin")
    for role in user_roles:
        if getattr(role, "name", "") == admin_role_name:
            is_admin = True
            break

    if is_admin:
        # Admins see all menu items (permission check always passes)
        allowed_menus: set[str] = set(all_names)
    else:
        # Non-admin: intersect tree names with user's menu_access perms.
        # This mirrors FAB's SecurityManager.get_user_menu_access() which
        # returns the set of menu names where the user has the
        # ``(menu_access, <name>)`` permission.
        user_perms: set[tuple[str, str]] = getattr(user, "permissions", set())
        allowed_menus = set()
        for name in all_names:
            if not name:
                # Empty-name items are always visible (shouldn't happen
                # with proper names, but defensive).
                allowed_menus.add(name)
                continue
            if ("menu_access", name) in user_perms:
                allowed_menus.add(name)

    return _get_data(menu_tree, allowed_menus)


class MenuController(Controller):
    """Menu API — ``GET /api/v1/menu/``.

    Port of FAB's ``MenuApi.get_menu_data`` from
    ``flask_appbuilder/menu.py``.

    Returns a forest-like menu structure filtered by the current user's
    ``menu_access`` permissions.
    """

    path = "/api/v1/menu"
    tags = ["Menu"]

    @get(
        "/",
        guards=[require_permission("can_get", "MenuApi")],
    )
    async def get_menu_data(
        self,
        request: Request[Any, Any, Any],
        state: State,
    ) -> dict[str, Any]:
        """Get the menu data structure.

        Returns a forest-like structure with the menu items the user
        has access to.  Mirrors FAB's ``GET /api/v1/menu/`` response::

            {"result": [<menu_items>]}
        """
        user = request.user
        settings = state.settings
        menu_items = _filter_menu_for_user(user, settings)
        return {"result": menu_items}
