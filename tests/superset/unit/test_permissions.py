"""Tests for permission constants and helper functions."""

from __future__ import annotations

from superset.security.permissions import (
    ADMIN_ONLY_PERMISSIONS,
    ALL_DATABASE_ACCESS,
    ALL_DATASOURCE_ACCESS,
    CAN_CSV,
    CAN_DELETE,
    CAN_EXPLORE,
    CAN_READ,
    CAN_SHARE_CHART,
    CAN_SHARE_DASHBOARD,
    CAN_SQLLAB,
    CAN_WRITE,
    CATALOG_ACCESS,
    DATABASE_ACCESS,
    DATASOURCE_ACCESS,
    READ_ONLY_PERMISSIONS,
    SCHEMA_ACCESS,
)


def test_database_access_constant():
    assert DATABASE_ACCESS == "database_access"


def test_schema_access_constant():
    assert SCHEMA_ACCESS == "schema_access"


def test_datasource_access_constant():
    assert DATASOURCE_ACCESS == "datasource_access"


def test_catalog_access_constant():
    assert CATALOG_ACCESS == "catalog_access"


def test_admin_only_permissions_is_frozenset():
    assert isinstance(ADMIN_ONLY_PERMISSIONS, frozenset)
    assert ADMIN_ONLY_PERMISSIONS == frozenset(
        {
            "update_roles_users",
            "list_roles",
            "can_update_role",
            "all_query_access",
            "can_grant_guest_token",
            "can_set_embedded",
            "can_warm_up_cache",
        }
    )


def test_read_only_permissions_is_frozenset():
    assert isinstance(READ_ONLY_PERMISSIONS, frozenset)
    assert CAN_READ in READ_ONLY_PERMISSIONS


def test_can_constants():
    assert CAN_READ == "can_read"
    assert CAN_WRITE == "can_write"
    assert CAN_DELETE == "can_delete"
    assert CAN_EXPLORE == "can_explore"
    assert CAN_SQLLAB == "can_sqllab"
    assert CAN_CSV == "can_csv"
    assert CAN_SHARE_DASHBOARD == "can_share_dashboard"
    assert CAN_SHARE_CHART == "can_share_chart"


def test_all_database_access_constant():
    assert ALL_DATABASE_ACCESS == "all_database_access"


def test_all_datasource_access_constant():
    assert ALL_DATASOURCE_ACCESS == "all_datasource_access"


def test_list_roles_pvm_is_admin_only():
    """``can_list_roles`` must live on the admin-only ``RoleRestAPI`` view menu.

    The original /security/roles/search endpoint is a SEPARATE class —
    ``RoleRestAPI`` (superset_old/security/api.py:199, ``@permission_name
    ("list_roles")`` → PVM ``can_list_roles on RoleRestAPI``), and
    "RoleRestAPI" is in ADMIN_ONLY_VIEW_MENUS (superset_old/security/
    manager.py:288). Registering the PVM under "SecurityRestApi" instead
    would leak role enumeration to Alpha/Gamma after a role sync.
    """
    from types import SimpleNamespace

    from superset.controllers.security import SecurityController
    from superset.security.permissions import ADMIN_ONLY_VIEW_MENUS
    from superset.security.sync_roles import (
        _is_admin_only,
        _STANDARD_VIEW_PERMISSIONS,
    )

    assert "RoleRestAPI" in ADMIN_ONLY_VIEW_MENUS
    assert ("can_list_roles", "RoleRestAPI") in _STANDARD_VIEW_PERMISSIONS
    assert ("can_list_roles", "SecurityRestApi") not in _STANDARD_VIEW_PERMISSIONS

    pvm = SimpleNamespace(
        permission=SimpleNamespace(name="can_list_roles"),
        view_menu=SimpleNamespace(name="RoleRestAPI"),
    )
    assert _is_admin_only(pvm)

    # The route guard must reference the same admin-only view menu.
    handler = SecurityController.search_roles
    guard_closures = [
        {
            str(cell.cell_contents)
            for cell in (guard.__closure__ or [])
            if isinstance(cell.cell_contents, (str, tuple))
        }
        for guard in (handler.guards or [])
    ]
    assert any(
        any("RoleRestAPI" in str(c) for c in closure) for closure in guard_closures
    ), "search_roles guard must check the RoleRestAPI view menu"
