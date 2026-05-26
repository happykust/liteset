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
"""Async role synchronisation -- ``superset init`` entry point.

Reimplements SupersetSecurityManager.sync_role_definitions() using
AsyncSession.  Creates / updates the built-in roles (Admin, Alpha,
Gamma, sql_lab) with the correct permission-view-menu sets, creates
custom permissions, handles PUBLIC_ROLE_LIKE, and cleans up faulty PVMs.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from superset.models.security import (
    Permission,
    PermissionView,
    Role,
    ViewMenu,
)
from superset.security.permissions import (
    ACCESSIBLE_PERMS,
    ADMIN_ONLY_PERMISSIONS,
    ADMIN_ONLY_VIEW_MENUS,
    ALPHA_ONLY_PERMISSIONS,
    ALPHA_ONLY_PMVS,
    ALPHA_ONLY_VIEW_MENUS,
    CUSTOM_PERMISSION_VIEWS,
    DATA_ACCESS_PERMISSIONS,
    GAMMA_READ_ONLY_MODEL_VIEWS,
    OBJECT_SPEC_PERMISSIONS,
    READ_ONLY_MODEL_VIEWS,
    READ_ONLY_PERMISSION,
    SQLLAB_EXTRA_PERMISSION_VIEWS,
    SQLLAB_ONLY_PERMISSIONS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PVM predicate functions
# ---------------------------------------------------------------------------


def _is_user_defined_permission(pvm: PermissionView) -> bool:
    """Return True if the PVM is user-defined (object-specific).

    Object-specific permissions (datasource_access, schema_access, etc.)
    are assigned per-object and excluded from role sync predicates.
    """
    return pvm.permission.name in OBJECT_SPEC_PERMISSIONS


def _is_admin_only(pvm: PermissionView) -> bool:
    """Return True if the PVM is accessible only to Admin users."""
    if (pvm.permission.name, pvm.view_menu.name) in ALPHA_ONLY_PMVS:
        return False
    if (
        pvm.view_menu.name in READ_ONLY_MODEL_VIEWS
        and pvm.permission.name not in READ_ONLY_PERMISSION
    ):
        return True
    return (
        pvm.view_menu.name in ADMIN_ONLY_VIEW_MENUS
        or pvm.permission.name in ADMIN_ONLY_PERMISSIONS
    )


def _is_alpha_only(pvm: PermissionView) -> bool:
    """Return True if the PVM is accessible only to Alpha (not Gamma)."""
    if (
        pvm.view_menu.name in GAMMA_READ_ONLY_MODEL_VIEWS
        and pvm.permission.name not in READ_ONLY_PERMISSION
    ):
        return True
    if (pvm.permission.name, pvm.view_menu.name) in ALPHA_ONLY_PMVS:
        return True
    return (
        pvm.view_menu.name in ALPHA_ONLY_VIEW_MENUS
        or pvm.permission.name in ALPHA_ONLY_PERMISSIONS
    )


def _is_accessible_to_all(pvm: PermissionView) -> bool:
    """Return True if the PVM is accessible to all authenticated users."""
    return pvm.permission.name in ACCESSIBLE_PERMS


def _is_sql_lab_only(pvm: PermissionView) -> bool:
    """Return True if the PVM is SQL-Lab-only."""
    return (pvm.permission.name, pvm.view_menu.name) in SQLLAB_ONLY_PERMISSIONS


# ---------------------------------------------------------------------------
# Role-membership predicates (mirrors SupersetSecurityManager)
# ---------------------------------------------------------------------------


def _is_admin_pvm(pvm: PermissionView) -> bool:
    """Admin gets every PVM except user-defined (object-specific) ones."""
    return not _is_user_defined_permission(pvm)


def _is_alpha_pvm(pvm: PermissionView) -> bool:
    """Alpha gets everything except Admin-only, sql-lab-only, and user-defined.

    Plus always gets ACCESSIBLE_PERMS.
    """
    return not (
        _is_user_defined_permission(pvm) or _is_admin_only(pvm) or _is_sql_lab_only(pvm)
    ) or _is_accessible_to_all(pvm)


def _is_gamma_pvm(pvm: PermissionView) -> bool:
    """Gamma gets everything except Admin-only, Alpha-only, sql-lab-only,
    and user-defined.  Plus always gets ACCESSIBLE_PERMS.
    """
    return not (
        _is_user_defined_permission(pvm)
        or _is_admin_only(pvm)
        or _is_alpha_only(pvm)
        or _is_sql_lab_only(pvm)
    ) or _is_accessible_to_all(pvm)


def _is_sql_lab_pvm(pvm: PermissionView) -> bool:
    """sql_lab role gets SQL-Lab-only + SQL-Lab-extra PVMs."""
    return (
        _is_sql_lab_only(pvm)
        or (pvm.permission.name, pvm.view_menu.name) in SQLLAB_EXTRA_PERMISSION_VIEWS
    )


# ---------------------------------------------------------------------------
# Helper: get or create a Permission row
# ---------------------------------------------------------------------------


async def _get_or_create_permission(session: AsyncSession, name: str) -> Permission:
    """Get an existing Permission or create one."""
    result = await session.execute(select(Permission).where(Permission.name == name))
    perm = result.scalars().one_or_none()
    if perm is None:
        perm = Permission(name=name)
        session.add(perm)
        await session.flush()
    return perm


# ---------------------------------------------------------------------------
# Helper: get or create a ViewMenu row
# ---------------------------------------------------------------------------


async def _get_or_create_view_menu(session: AsyncSession, name: str) -> ViewMenu:
    """Get an existing ViewMenu or create one."""
    result = await session.execute(select(ViewMenu).where(ViewMenu.name == name))
    vm = result.scalars().one_or_none()
    if vm is None:
        vm = ViewMenu(name=name)
        session.add(vm)
        await session.flush()
    return vm


# ---------------------------------------------------------------------------
# Helper: get or create a PermissionView (permission + view_menu pair)
# ---------------------------------------------------------------------------


async def _get_or_create_pvm(
    session: AsyncSession,
    permission_name: str,
    view_menu_name: str,
) -> PermissionView:
    """Get an existing PermissionView or create one."""
    perm = await _get_or_create_permission(session, permission_name)
    vm = await _get_or_create_view_menu(session, view_menu_name)
    result = await session.execute(
        select(PermissionView).where(
            PermissionView.permission_id == perm.id,
            PermissionView.view_menu_id == vm.id,
        )
    )
    pvm = result.scalars().one_or_none()
    if pvm is None:
        pvm = PermissionView(permission_id=perm.id, view_menu_id=vm.id)
        session.add(pvm)
        await session.flush()
    return pvm


# ---------------------------------------------------------------------------
# Helper: get or create a Role
# ---------------------------------------------------------------------------


async def _get_or_create_role(session: AsyncSession, name: str) -> Role:
    """Get an existing Role or create one.

    Always returns a Role with the ``permissions`` collection eagerly
    loaded so that subsequent assignment (``role.permissions = [...]``)
    doesn't trigger a synchronous lazy-load in async context.
    """
    result = await session.execute(
        select(Role).where(Role.name == name).options(selectinload(Role.permissions))
    )
    role = result.scalars().one_or_none()
    if role is None:
        role = Role(name=name)
        session.add(role)
        await session.flush()
        # Re-fetch so that the permissions collection is populated
        # by selectinload (avoids MissingGreenlet on assignment).
        result = await session.execute(
            select(Role)
            .where(Role.id == role.id)
            .options(selectinload(Role.permissions))
        )
        role = result.scalars().one()
    return role


# ---------------------------------------------------------------------------
# Helper: load all PVMs with eager-loaded relationships
# ---------------------------------------------------------------------------


async def _get_all_pvms(session: AsyncSession) -> list[PermissionView]:
    """Load all PermissionView rows with permission and view_menu."""
    result = await session.execute(
        select(PermissionView).options(
            selectinload(PermissionView.permission),
            selectinload(PermissionView.view_menu),
        )
    )
    pvms = result.scalars().all()
    return [p for p in pvms if p.permission and p.view_menu]


# ---------------------------------------------------------------------------
# Helper: set_role — assign filtered PVMs to a role
# ---------------------------------------------------------------------------


async def _set_role(
    session: AsyncSession,
    role_name: str,
    pvm_check: Callable[[PermissionView], bool],
    pvms: list[PermissionView],
) -> Role:
    """Create/update a role with the PVMs passing the predicate."""
    logger.info("Syncing %s perms", role_name)
    role = await _get_or_create_role(session, role_name)
    role.permissions = [pvm for pvm in pvms if pvm_check(pvm)]
    return role


# ---------------------------------------------------------------------------
# Helper: copy_role — PUBLIC_ROLE_LIKE support
# ---------------------------------------------------------------------------


async def _copy_role(
    session: AsyncSession,
    source_role_name: str,
    target_role_name: str,
    *,
    merge: bool = True,
) -> None:
    """Copy permissions from one role to another.

    If ``merge`` is True, existing data-access permissions on the
    target role are preserved (not overwritten).
    """
    logger.info("Copy/Merge %s to %s", source_role_name, target_role_name)

    source_result = await session.execute(
        select(Role)
        .where(Role.name == source_role_name)
        .options(selectinload(Role.permissions).selectinload(PermissionView.permission))
    )
    source_role = source_result.scalars().one_or_none()
    if source_role is None:
        logger.warning("Source role '%s' not found, skipping copy.", source_role_name)
        return

    target_role = await _get_or_create_role(session, target_role_name)

    source_pvms = list(source_role.permissions)

    if merge:
        # Preserve existing data-access permissions on target
        for pvm in target_role.permissions:
            if (
                pvm not in source_pvms
                and pvm.permission
                and pvm.permission.name in DATA_ACCESS_PERMISSIONS
            ):
                source_pvms.append(pvm)

    target_role.permissions = source_pvms


# ---------------------------------------------------------------------------
# Helper: create_missing_perms for datasources and databases
# ---------------------------------------------------------------------------

_STANDARD_VIEW_PERMISSIONS: list[tuple[str, str]] = [
    # Core resources (can_read + can_write)
    ("can_read", "Chart"),
    ("can_write", "Chart"),
    ("can_read", "Dashboard"),
    ("can_write", "Dashboard"),
    ("can_read", "Database"),
    ("can_write", "Database"),
    ("can_read", "Dataset"),
    ("can_write", "Dataset"),
    ("can_read", "Query"),
    ("can_write", "Query"),
    ("can_read", "SavedQuery"),
    ("can_write", "SavedQuery"),
    ("can_read", "ReportSchedule"),
    ("can_write", "ReportSchedule"),
    ("can_read", "Annotation"),
    ("can_write", "Annotation"),
    ("can_read", "CssTemplate"),
    ("can_write", "CssTemplate"),
    ("can_read", "Tag"),
    ("can_write", "Tag"),
    ("can_read", "Explore"),
    ("can_read", "Datasource"),
    ("can_read", "EmbeddedDashboard"),
    ("can_write", "EmbeddedDashboard"),
    ("can_read", "AvailableDomains"),
    ("can_read", "AdvancedDataType"),
    ("can_read", "DynamicPlugin"),
    ("can_write", "DynamicPlugin"),
    ("can_read", "Theme"),
    ("can_write", "Theme"),
    ("can_read", "Row Level Security"),
    ("can_write", "Row Level Security"),
    # SQL Lab
    # Custom Superset-view permissions (mirror create_custom_permissions in
    # superset_old/security/manager.py:1109-1124 — these live on the
    # ``Superset`` view-menu, NOT ``SqlLab``).
    ("can_sqllab", "Superset"),
    ("can_sqllab_history", "Superset"),
    ("can_csv", "Superset"),
    ("can_share_dashboard", "Superset"),
    ("can_share_chart", "Superset"),
    # SqlLabRestApi (class_permission_name="SQLLab") + fine-grained method
    # permissions FAB auto-creates for the SQL Lab endpoints. Mirrors
    # SQLLAB_ONLY_PERMISSIONS in superset_old/security/manager.py:351-381.
    ("can_read", "SQLLab"),
    ("can_write", "SQLLab"),
    ("can_execute_sql_query", "SQLLab"),
    ("can_get_results", "SQLLab"),
    ("can_export_csv", "SQLLab"),
    ("can_estimate_query_cost", "SQL Lab"),
    ("can_export_csv", "Query"),
    # Menu access
    ("menu_access", "Dashboards"),
    ("menu_access", "Charts"),
    ("menu_access", "SQL Lab"),
    ("menu_access", "Data"),
    ("menu_access", "Databases"),
    ("menu_access", "Datasets"),
    ("menu_access", "Manage"),
    ("menu_access", "Annotation Layers"),
    ("menu_access", "CSS Templates"),
    ("menu_access", "Import Dashboards"),
    ("menu_access", "Query Search"),
    ("menu_access", "Saved Queries"),
    ("menu_access", "Security"),
    ("menu_access", "Tags"),
    ("menu_access", "Upload a CSV"),
    # Security
    ("can_grant_guest_token", "SecurityRestApi"),
    ("can_read", "SecurityRestApi"),
    ("can_list_roles", "SecurityRestApi"),
    # FAB security CRUD REST APIs (AB_ADD_SECURITY_API). FAB's ModelRestApi
    # uses per-HTTP-method permission names (can_get/can_post/can_put/
    # can_delete/can_info), not Superset's can_read/can_write. These
    # resources are all in ADMIN_ONLY_VIEW_MENUS so the PVMs are granted to
    # Admin only. See flask_appbuilder/security/sqla/apis/*/api.py.
    ("can_get", "User"),
    ("can_post", "User"),
    ("can_put", "User"),
    ("can_delete", "User"),
    ("can_info", "User"),
    ("can_get", "Role"),
    ("can_post", "Role"),
    ("can_put", "Role"),
    ("can_delete", "Role"),
    ("can_info", "Role"),
    # RoleApi sub-resource handlers use custom @permission_name overrides.
    ("can_list_role_permissions", "Role"),
    ("can_add_role_permissions", "Role"),
    ("can_update_role_users", "Role"),
    ("can_update_role_groups", "Role"),
    # PermissionApi is read-only (include_route_methods={"info","get","get_list"}).
    ("can_get", "Permission"),
    ("can_info", "Permission"),
    ("can_get", "ViewMenu"),
    ("can_post", "ViewMenu"),
    ("can_put", "ViewMenu"),
    ("can_delete", "ViewMenu"),
    ("can_info", "ViewMenu"),
    ("can_get", "PermissionViewMenu"),
    ("can_post", "PermissionViewMenu"),
    ("can_put", "PermissionViewMenu"),
    ("can_delete", "PermissionViewMenu"),
    ("can_info", "PermissionViewMenu"),
    ("can_get", "Group"),
    ("can_post", "Group"),
    ("can_put", "Group"),
    ("can_delete", "Group"),
    ("can_info", "Group"),
    ("can_userinfo", "UserInfoView"),
    ("can_read", "UserMeRestApi"),
    ("can_write", "UserMeRestApi"),
    ("resetmypassword", "UserInfoView"),
    ("can_recent_activity", "Log"),
    ("can_read", "Log"),
    ("can_write", "Log"),
    # Data access (special)
    ("all_datasource_access", "all_datasource_access"),
    ("all_database_access", "all_database_access"),
    ("all_query_access", "all_query_access"),
    # Cache
    ("can_read", "CacheRestApi"),
    ("can_write", "CacheRestApi"),
    # Dashboard filter state / permalink
    ("can_read", "DashboardFilterStateRestApi"),
    ("can_write", "DashboardFilterStateRestApi"),
    ("can_read", "DashboardPermalinkRestApi"),
    ("can_write", "DashboardPermalinkRestApi"),
    # Explore form data / permalink
    ("can_read", "ExploreFormDataRestApi"),
    ("can_write", "ExploreFormDataRestApi"),
    ("can_read", "ExplorePermalinkRestApi"),
    ("can_write", "ExplorePermalinkRestApi"),
    # Tab state
    ("can_read", "TabStateView"),
    ("can_write", "TabStateView"),
]


async def _create_missing_perms(session: AsyncSession) -> None:  # noqa: C901
    """Create missing PermissionView rows.

    1. Standard view permissions (can_read/can_write on all views)
    2. Datasource/database data-access permissions
    """
    logger.info("Fetching all existing PVMs for lookup")

    existing_pvms: set[tuple[str, str]] = set()
    all_pvms = await _get_all_pvms(session)
    for pvm in all_pvms:
        existing_pvms.add((pvm.permission.name, pvm.view_menu.name))

    created = 0

    # 1. Standard view permissions
    for perm_name, view_name in _STANDARD_VIEW_PERMISSIONS:
        if (perm_name, view_name) not in existing_pvms:
            await _get_or_create_pvm(
                session,
                perm_name,
                view_name,
            )
            existing_pvms.add((perm_name, view_name))
            created += 1

    logger.info(
        "Created %d standard view PVMs",
        created,
    )

    # 2. Datasource permissions
    try:
        from superset.models.sql_lab import SavedQuery  # noqa: F401

        # Try loading SqlaTable if available
        try:
            from superset.models.connectors import SqlaTable

            result = await session.execute(select(SqlaTable))
            datasources = result.scalars().all()
            for ds in datasources:
                perm = getattr(ds, "perm", None)
                if perm and ("datasource_access", perm) not in existing_pvms:
                    await _get_or_create_pvm(session, "datasource_access", perm)
                    existing_pvms.add(("datasource_access", perm))
                    created += 1
                schema_perm = getattr(ds, "schema_perm", None)
                if schema_perm and ("schema_access", schema_perm) not in existing_pvms:
                    await _get_or_create_pvm(session, "schema_access", schema_perm)
                    existing_pvms.add(("schema_access", schema_perm))
                    created += 1
                catalog_perm = getattr(ds, "catalog_perm", None)
                if (
                    catalog_perm
                    and ("catalog_access", catalog_perm) not in existing_pvms
                ):
                    await _get_or_create_pvm(session, "catalog_access", catalog_perm)
                    existing_pvms.add(("catalog_access", catalog_perm))
                    created += 1
        except ImportError:
            logger.debug("SqlaTable model not available, skipping datasource perms")
    except ImportError:
        pass

    # Database permissions
    try:
        from superset.models.core import Database

        result = await session.execute(select(Database))
        databases = result.scalars().all()
        for db_obj in databases:
            perm = getattr(db_obj, "perm", None)
            if perm and ("database_access", perm) not in existing_pvms:
                await _get_or_create_pvm(session, "database_access", perm)
                existing_pvms.add(("database_access", perm))
                created += 1
    except ImportError:
        logger.debug("Database model not available, skipping database perms")

    if created:
        logger.info("Created %d missing permission-view entries", created)


# ---------------------------------------------------------------------------
# Helper: clean_perms — remove faulty PermissionView rows
# ---------------------------------------------------------------------------


async def _clean_perms(session: AsyncSession) -> None:
    """Delete PermissionView rows with NULL permission or view_menu."""
    logger.info("Cleaning faulty permissions")

    result = await session.execute(
        select(PermissionView).where(
            or_(
                PermissionView.permission_id.is_(None),
                PermissionView.view_menu_id.is_(None),
            )
        )
    )
    faulty = result.scalars().all()
    if faulty:
        for pvm in faulty:
            await session.delete(pvm)
        logger.info("Deleted %d faulty permissions", len(faulty))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def sync_role_definitions(
    session: AsyncSession,
    *,
    public_role_like: str | None = None,
) -> dict[str, Any]:
    """Create/update Admin, Alpha, Gamma, sql_lab roles with correct permissions.

    This is the async equivalent of
    ``SupersetSecurityManager.sync_role_definitions()``.

    Args:
        session: An async database session.
        public_role_like: If set (e.g. "Gamma"), the Public role
            will be configured with the same permissions as the named role.

    Returns:
        A summary dict with counts of created/updated roles and permissions.
    """
    logger.info("Syncing role definitions")

    # Step 1: Create standard + custom PVMs (idempotent)
    await _create_missing_perms(session)
    for perm_name, view_name in CUSTOM_PERMISSION_VIEWS:
        await _get_or_create_pvm(session, perm_name, view_name)

    await session.flush()

    # Step 2: Load all PVMs (now includes standard ones)
    pvms = await _get_all_pvms(session)

    # Step 3: Create/update default roles
    admin_role = await _set_role(session, "Admin", _is_admin_pvm, pvms)
    alpha_role = await _set_role(session, "Alpha", _is_alpha_pvm, pvms)
    gamma_role = await _set_role(session, "Gamma", _is_gamma_pvm, pvms)
    sql_lab_role = await _set_role(
        session,
        "sql_lab",
        _is_sql_lab_pvm,
        pvms,
    )

    # Step 4: Ensure Public role exists
    public_role = await _get_or_create_role(session, "Public")

    # Step 5: Handle PUBLIC_ROLE_LIKE
    if public_role_like:
        await _copy_role(
            session,
            public_role_like,
            "Public",
            merge=True,
        )

    # Step 6: Clean up faulty PVMs
    await _clean_perms(session)

    # Commit all changes
    await session.flush()

    summary = {
        "roles_synced": ["Admin", "Alpha", "Gamma", "sql_lab", "Public"],
        "admin_permissions": len(admin_role.permissions),
        "alpha_permissions": len(alpha_role.permissions),
        "gamma_permissions": len(gamma_role.permissions),
        "sql_lab_permissions": len(sql_lab_role.permissions),
        "public_permissions": len(public_role.permissions),
        "total_pvms": len(pvms),
    }
    logger.info("Role sync complete: %s", summary)
    return summary
