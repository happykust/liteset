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
    assert "can_grant_access" in ADMIN_ONLY_PERMISSIONS


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
