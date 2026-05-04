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
"""Object-level access filters for superset controllers.

Each filter function returns a list of SQLAlchemy WHERE clauses that restrict
a query to only the objects the current user can access.  An empty list means
no restriction (the user can see everything).

The returned clauses are passed directly to ``BaseAsyncDAO.find_all(filters=...)``
and ``BaseAsyncDAO.count(filters=...)``.

All Superset model imports are lazy (inside function bodies) to avoid
triggering the Flask import chain at module load time.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select

logger = logging.getLogger(__name__)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_uuid(value: Any) -> bool:
    """Return True if ``value`` looks like a stringified UUID.

    Mirrors ``superset_old.models.dashboard.is_uuid`` which is used by the
    original ``DashboardAccessFilter`` to choose between filtering embedded
    UUIDs vs integer ids.
    """
    if isinstance(value, UUID):
        return True
    if not isinstance(value, str):
        return False
    return bool(_UUID_RE.match(value))


async def chart_access_filters(
    security_manager: Any,
    user: Any,
) -> list[Any]:
    """Return SQLAlchemy filters restricting charts to those the user can access.

    Mirrors Superset's ChartFilter: admins see all, others see only charts
    whose datasources they have permission to access.
    """
    if security_manager.is_admin(user):
        return []

    # If the user has the global ``all_datasource_access`` permission, no
    # restriction (1:1 with ``ChartFilter`` which short-circuits in the
    # same case).
    can_access_all = False
    can_access_all_method = getattr(
        security_manager, "can_access_all_datasources", None
    )
    if can_access_all_method is not None:
        can_access_all = await can_access_all_method(user=user)
    if can_access_all:
        return []

    # Get datasource IDs user can access
    accessible_datasource_ids = await security_manager.get_accessible_datasource_ids(
        user
    )
    if accessible_datasource_ids is None:
        # Method not implemented yet -- fall back to no filtering (permissive)
        return []

    try:
        from superset.models.slice import Slice

        return [Slice.datasource_id.in_(accessible_datasource_ids)]
    except (ImportError, ModuleNotFoundError):
        return []


async def dashboard_access_filters(  # noqa: C901  # full 1:1 port from old code
    security_manager: Any,
    user: Any,
) -> list[Any]:
    """Return SQLAlchemy filters restricting dashboards to those the user can access.

    1:1 port of ``superset_old/dashboards/filters.py:DashboardAccessFilter``.

    Visibility rules for non-admin users:
      1. dashboards the user owns (via ``Dashboard.owners`` M2M);
      2. published dashboards whose every chart's datasource the user can
         access (via ``get_dataset_access_filters`` / ``Slice.perm`` /
         ``schema_perm`` / ``catalog_perm`` / ``Database.id`` join);
      3. when ``DASHBOARD_RBAC`` feature is enabled — dashboards explicitly
         shared with one of the user's roles, AND the dashboard must be
         published;
      4. when ``EMBEDDED_SUPERSET`` is enabled and the current user is a
         guest — dashboards whose UUID is in the guest's resources list.

    Admins see all dashboards.

    The output mirrors the original ``DashboardAccessFilter.apply`` so
    existing SQL-trace based test fixtures and observability dashboards
    continue to match.
    """
    if security_manager.is_admin(user):
        return []

    try:
        from superset.models.connectors import SqlaTable
        from superset.models.core import Database
        from superset.models.dashboard import Dashboard
        from superset.models.embedded_dashboard import EmbeddedDashboard
        from superset.models.slice import Slice
    except (ImportError, ModuleNotFoundError):
        return []

    from superset.utils.feature_flags import feature_flag_manager

    rbac_enabled = feature_flag_manager.is_feature_enabled("DASHBOARD_RBAC")
    embedded_enabled = feature_flag_manager.is_feature_enabled("EMBEDDED_SUPERSET")

    # ------------------------------------------------------------------
    # Build the dataset-permission filter expression that mirrors the
    # original ``superset_old/utils/filters.py:get_dataset_access_filters``.
    # ------------------------------------------------------------------
    can_access_all_datasources = await security_manager.can_access_all_datasources(
        user=user
    )
    accessible_database_ids = await security_manager.get_accessible_database_ids(user)
    datasource_perms = await security_manager.user_view_menu_names(
        "datasource_access", user=user
    )
    schema_perms = await security_manager.user_view_menu_names(
        "schema_access", user=user
    )
    catalog_perms = await security_manager.user_view_menu_names(
        "catalog_access", user=user
    )

    if can_access_all_datasources:
        # Original short-circuits: ``get_dataset_access_filters`` returns a
        # non-restrictive ``or_()`` clause; building the OR with a constant
        # ``True`` here keeps the SQL identical to "no dataset restriction".
        dataset_access_clause: Any = True
    else:
        dataset_access_clause = or_(
            Database.id.in_(accessible_database_ids or []),
            Slice.perm.in_(datasource_perms),
            Slice.catalog_perm.in_(catalog_perms),
            Slice.schema_perm.in_(schema_perms),
        )

    # ------------------------------------------------------------------
    # Dataset-perm subquery (Branch 2 of the original filter).
    # ``isouter=True`` join on slices so dashboards without any slice still
    # surface (they would never match the perm clause but the LEFT JOIN
    # parity matches the original SQL trace).
    # ------------------------------------------------------------------
    is_rbac_disabled_filter: list[Any] = []
    dashboard_has_roles = Dashboard.roles.any()
    if rbac_enabled:
        # When DASHBOARD_RBAC is enabled, the published-dashboard branch
        # additionally excludes dashboards that have explicit roles —
        # those are gated on the RBAC roles_based_query branch instead.
        is_rbac_disabled_filter.append(~dashboard_has_roles)

    datasource_perm_query = (
        select(Dashboard.id)
        .join(Dashboard.slices, isouter=True)
        .join(SqlaTable, Slice.datasource_id == SqlaTable.id)
        .join(Database, SqlaTable.database_id == Database.id)
        .where(
            and_(
                Dashboard.published.is_(True),
                *is_rbac_disabled_filter,
                dataset_access_clause,
            )
        )
    )

    # ------------------------------------------------------------------
    # Owner-id subquery (Branch 1).
    # ------------------------------------------------------------------
    user_id = getattr(user, "id", None)
    if user_id is None:
        owner_ids_query: Any = select(Dashboard.id).where(Dashboard.id == -1)
    else:
        owner_ids_query = (
            select(Dashboard.id)
            .join(Dashboard.owners)
            .where(Dashboard.owners.any(id=user_id))
        )

    feature_flagged_filters: list[Any] = []

    # ------------------------------------------------------------------
    # DASHBOARD_RBAC role-based branch (Branch 3).
    # ------------------------------------------------------------------
    if rbac_enabled:
        user_roles = await security_manager.get_user_roles(user)
        user_role_ids = [getattr(r, "id", None) for r in (user_roles or [])]
        user_role_ids = [rid for rid in user_role_ids if rid is not None]

        # Use the association table directly — avoids depending on FAB's
        # ``ab_role`` model class import.
        try:
            from superset.models.dashboard import DashboardRoles  # type: ignore

            roles_based_query = (
                select(Dashboard.id)
                .join(DashboardRoles, DashboardRoles.c.dashboard_id == Dashboard.id)
                .where(
                    and_(
                        Dashboard.published.is_(True),
                        dashboard_has_roles,
                        DashboardRoles.c.role_id.in_(user_role_ids),
                    )
                )
            )
        except (ImportError, AttributeError):
            # Fallback: use the relationship-based subquery directly.
            from sqlalchemy.orm import aliased

            role_alias = aliased(Dashboard.roles.property.mapper.class_)
            roles_based_query = (
                select(Dashboard.id)
                .join(Dashboard.roles)
                .where(
                    and_(
                        Dashboard.published.is_(True),
                        dashboard_has_roles,
                        role_alias.id.in_(user_role_ids),
                    )
                )
            )

        feature_flagged_filters.append(Dashboard.id.in_(roles_based_query))

    # ------------------------------------------------------------------
    # EMBEDDED_SUPERSET guest-user branch (Branch 4).
    # ------------------------------------------------------------------
    if embedded_enabled and security_manager.is_guest_user(user):
        # The guest user carries its dashboard resources in ``user.resources``
        # (see ``superset/security/guest.py``).  Each resource is
        # ``{"type": "dashboard", "id": "<id-or-uuid>"}``.
        embedded_dashboard_ids: list[Any] = [
            r["id"]
            for r in (getattr(user, "resources", None) or [])
            if isinstance(r, dict) and r.get("type") == "dashboard"
        ]

        # TODO (embedded): only use uuid filter once uuids are rolled out.
        # 1:1 with the original ``superset_old/dashboards/filters.py``.
        if any(_is_uuid(rid) for rid in embedded_dashboard_ids):
            condition = Dashboard.embedded.any(
                EmbeddedDashboard.uuid.in_(embedded_dashboard_ids)
            )
        else:
            # Cast non-UUID resource ids to int when possible — the original
            # passes them through to ``Dashboard.id.in_(...)`` directly,
            # which works because the legacy code path used numeric ids.
            int_ids: list[int] = []
            for rid in embedded_dashboard_ids:
                if isinstance(rid, int):
                    int_ids.append(rid)
                elif isinstance(rid, str) and rid.isdigit():
                    int_ids.append(int(rid))
            condition = Dashboard.id.in_(int_ids)

        feature_flagged_filters.append(condition)

    # ------------------------------------------------------------------
    # Combine all branches.  Original is a single OR over four subqueries.
    # ------------------------------------------------------------------
    return [
        or_(
            Dashboard.id.in_(owner_ids_query),
            Dashboard.id.in_(datasource_perm_query),
            *feature_flagged_filters,
        )
    ]


async def dataset_access_filters(
    security_manager: Any,
    user: Any,
) -> list[Any]:
    """Return SQLAlchemy filters restricting datasets to those the user can access.

    Mirrors Superset's DatasourceFilter: admins see all, others see datasets
    on databases they can access.
    """
    if security_manager.is_admin(user):
        return []

    accessible_db_ids = await security_manager.get_accessible_database_ids(user)
    if accessible_db_ids is None:
        return []

    try:
        from superset.models.connectors import SqlaTable

        return [SqlaTable.database_id.in_(accessible_db_ids)]
    except (ImportError, ModuleNotFoundError):
        return []


async def query_access_filters(
    security_manager: Any,
    user: Any,
) -> list[Any]:
    """Return SQLAlchemy filters restricting queries to user's own.

    Mirrors Superset's QueryFilter: admins/can_access_all_queries see all,
    others see only their own queries.
    """
    if security_manager.is_admin(user):
        return []

    can_access_all = await security_manager.can_access(
        "can_access_all_queries", "Superset", user=user
    )
    if can_access_all:
        return []

    user_id = getattr(user, "id", None)
    if user_id is None:
        try:
            from superset.models.sql_lab import Query

            return [Query.user_id == -1]  # No access
        except (ImportError, ModuleNotFoundError):
            return []

    try:
        from superset.models.sql_lab import Query

        return [Query.user_id == user_id]
    except (ImportError, ModuleNotFoundError):
        return []


async def saved_query_access_filters(
    security_manager: Any,
    user: Any,
) -> list[Any]:
    """Return SQLAlchemy filters restricting saved queries.

    Admins and users with can_access_all_queries see all saved queries.
    Others see only their own (matched by created_by_fk from AuditMixinNullable).
    """
    if security_manager.is_admin(user):
        return []

    can_access_all = await security_manager.can_access(
        "can_access_all_queries", "Superset", user=user
    )
    if can_access_all:
        return []

    user_id = getattr(user, "id", None)
    if user_id is None:
        try:
            from superset.models.sql_lab import SavedQuery

            return [SavedQuery.created_by_fk == -1]
        except (ImportError, ModuleNotFoundError):
            return []

    try:
        from superset.models.sql_lab import SavedQuery

        return [SavedQuery.created_by_fk == user_id]
    except (ImportError, ModuleNotFoundError):
        return []
