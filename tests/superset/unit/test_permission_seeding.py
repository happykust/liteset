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
"""Every permission pair the code checks must actually be seeded.

Flask-AppBuilder materialised a ``permission_view`` row for every registered
view by introspection.  This port seeds from a literal list instead, so a pair
that a guard checks but the list omits exists in no role at all — not even
Admin.  That stayed invisible only because ``has_access`` short-circuits on the
Admin role; the moment that short-circuit is reconsidered, every unseeded
endpoint denies everyone.

This test walks the source for checked pairs and fails when one is unseeded.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any

from superset.security import sync_roles
from superset.security.permissions import CUSTOM_PERMISSION_VIEWS
from superset.security.sync_roles import _STANDARD_VIEW_PERMISSIONS

SUPERSET_ROOT = pathlib.Path(sync_roles.__file__).resolve().parents[1]

#: Calls whose first two string arguments are an ``(action, resource)`` pair.
_PERMISSION_CALLS = frozenset({"require_permission", "has_access", "can_access"})


def _checked_pairs() -> dict[tuple[str, str], str]:
    """Return every literal ``(action, resource)`` pair the source checks."""
    found: dict[tuple[str, str], str] = {}
    for path in SUPERSET_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name not in _PERMISSION_CALLS:
                continue
            action, resource = node.args[0], node.args[1]
            if (
                isinstance(action, ast.Constant)
                and isinstance(resource, ast.Constant)
                and isinstance(action.value, str)
                and isinstance(resource.value, str)
            ):
                key = (action.value, resource.value)
                found.setdefault(
                    key,
                    f"{path.relative_to(SUPERSET_ROOT.parent)}:{node.lineno}",
                )
    return found


def test_every_checked_permission_pair_is_seeded() -> None:
    seeded = set(_STANDARD_VIEW_PERMISSIONS) | set(CUSTOM_PERMISSION_VIEWS)
    missing = {
        pair: site for pair, site in _checked_pairs().items() if pair not in seeded
    }
    assert not missing, (
        "These permission pairs are checked by a guard or can_access() call but "
        "are never created by role seeding, so no role holds them — including "
        "Admin:\n"
        + "\n".join(
            f"  {a} on {r}  ({site})" for (a, r), site in sorted(missing.items())
        )
    )


def _pvm(action: str, resource: str) -> Any:
    return SimpleNamespace(
        permission=SimpleNamespace(name=action),
        view_menu=SimpleNamespace(name=resource),
    )


def test_admin_only_operations_stay_off_alpha_and_gamma() -> None:
    """Cache warm-up and embedding are admin-only upstream; keep them so."""
    for action, resource in (
        ("can_warm_up_cache", "Chart"),
        ("can_warm_up_cache", "Dataset"),
        ("can_set_embedded", "Dashboard"),
    ):
        pvm = _pvm(action, resource)
        assert sync_roles._is_admin_pvm(pvm) is True, (action, resource)
        assert sync_roles._is_alpha_pvm(pvm) is False, (action, resource)
        assert sync_roles._is_gamma_pvm(pvm) is False, (action, resource)


def test_registration_api_is_admin_only() -> None:
    """Activation tokens must not be reachable by Gamma.

    Upstream leaves this open because FAB derives the view menu from the class
    name and only the *menu* label is admin-listed; see the note in
    ``superset/security/permissions.py``.
    """
    for action in ("can_read", "can_write"):
        pvm = _pvm(action, "UserRegistrationsRestAPI")
        assert sync_roles._is_gamma_pvm(pvm) is False
        assert sync_roles._is_alpha_pvm(pvm) is False


def test_legacy_superset_views_reach_gamma() -> None:
    """``@has_access`` on Superset.dashboard is gamma-accessible upstream."""
    for action in ("can_dashboard", "can_log", "can_language_pack"):
        assert sync_roles._is_gamma_pvm(_pvm(action, "Superset")) is True
