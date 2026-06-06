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

    1:1 with ``superset_old/charts/filters.py::ChartFilter.apply`` +
    ``get_dataset_access_filters(Slice)``: admins / ``can_access_all_datasources``
    see all; everyone else sees charts whose datasource lives in an accessible
    database OR whose denormalized ``perm``/``catalog_perm``/``schema_perm``
    matches a held grant.

    The previous port filtered ONLY on ``Slice.datasource_id.in_(
    get_accessible_datasource_ids(...))`` — i.e. per-dataset ``datasource_access``
    perms — dropping the ``database_access``/``schema_access``/``catalog_access``
    branches. A non-admin whose chart access derives from a per-DB / schema /
    catalog grant (but no per-dataset perm) got an EMPTY id list → charts
    vanished from lists AND returned a spurious 404 on ``GET /chart/{id}`` and
    ``/chart/{id}/data/`` (these use this filter as the lookup gate). The sibling
    ``dataset_access_filters`` / ``dashboard_access_filters`` already carry the
    full OR; this helper was missed in the same RBAC churn.
    """
    if security_manager.is_admin(user):
        return []
    if await security_manager.can_access_all_datasources(user=user):
        return []

    from sqlalchemy import or_, select

    from superset.models.connectors import SqlaTable
    from superset.models.slice import Slice

    accessible_db_ids = await security_manager.get_accessible_database_ids(user) or []
    datasource_perms = await security_manager.user_view_menu_names(
        "datasource_access", user=user
    )
    schema_perms = await security_manager.user_view_menu_names(
        "schema_access", user=user
    )
    catalog_perms = await security_manager.user_view_menu_names(
        "catalog_access", user=user
    )

    # Subquery stands in for upstream's Slice→SqlaTable→Database join: charts
    # whose datasource lives in an accessible database.
    db_table_ids = select(SqlaTable.id).where(
        SqlaTable.database_id.in_(accessible_db_ids)
    )
    return [
        or_(
            Slice.datasource_id.in_(db_table_ids),
            Slice.perm.in_(datasource_perms),
            Slice.catalog_perm.in_(catalog_perms),
            Slice.schema_perm.in_(schema_perms),
        )
    ]


def _databases_from_view_menus(view_menu_names: set[str]) -> set[str]:
    """Extract database names from a set of ``view_menu`` permission strings.

    1:1 port of ``superset_old/databases/filters.py:can_access_databases``:
    a ``catalog_access`` / ``schema_access`` / ``datasource_access`` view menu
    looks like ``[db_name].[schema]`` (or ``[db_name].[schema].[table](id:N)``);
    the database name is the first dotted component with its surrounding
    brackets stripped (``vm.split(".")[0][1:-1]``).
    """
    result: set[str] = set()
    for vm in view_menu_names:
        head = vm.split(".")[0]
        # Strip the surrounding ``[...]`` brackets, matching the original
        # ``[1:-1]`` slice.  Guard against malformed entries.
        if len(head) >= 2:
            result.add(head[1:-1])
    return result


async def database_access_filters(
    security_manager: Any,
    user: Any,
) -> list[Any]:
    """Return SQLAlchemy filters restricting databases to those the user can access.

    1:1 port of ``superset_old/databases/filters.py:DatabaseFilter.apply``.

    Visibility rules:
      * users with the global ``all_database_access`` permission (which
        includes admins, who also carry that grant) see every database —
        no restriction;
      * everyone else is restricted to databases for which they hold a
        ``database_access`` permission OR whose name appears in any
        ``catalog_access`` / ``schema_access`` / ``datasource_access``
        view-menu permission.

    Note: the original additionally applies
    ``EXTRA_DYNAMIC_QUERY_FILTERS["databases"]`` to the FAB ``Query``.  That
    hook operates on a synchronous SQLAlchemy ``Query`` object and has no
    direct analogue in the async ``find_all(filters=...)`` clause-list API,
    so it is intentionally omitted here (no dynamic-filter callable is wired
    up in this deployment).
    """
    if await security_manager.can_access_all_databases(user=user):
        return []

    from superset.models.core import Database

    database_perms = await security_manager.user_view_menu_names(
        "database_access", user=user
    )
    catalog_access = await security_manager.user_view_menu_names(
        "catalog_access", user=user
    )
    schema_access = await security_manager.user_view_menu_names(
        "schema_access", user=user
    )
    datasource_access = await security_manager.user_view_menu_names(
        "datasource_access", user=user
    )
    database_names = (
        _databases_from_view_menus(catalog_access)
        | _databases_from_view_menus(schema_access)
        | _databases_from_view_menus(datasource_access)
    )

    return [
        or_(
            Database.perm.in_(database_perms),
            Database.database_name.in_(sorted(database_names)),
        )
    ]


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

    1:1 with ``superset_old/views/base.py::DatasourceFilter.apply`` +
    ``superset_old/utils/filters.py::get_dataset_access_filters``:

    * ``can_access_all_datasources()`` (admin / all_datasource_access /
      all_database_access) → no filter (see everything). The port previously
      only short-circuited on ``is_admin`` then filtered by
      ``get_accessible_database_ids``, which returns ``[]`` for a user whose
      access comes from the GLOBAL ``all_database_access`` perm (no per-DB
      ``[db].(id:N)`` grant) → ``database_id.in_([])`` → ZERO datasets. That
      wrongly hid every dataset from the stock Alpha role.
    * otherwise: ``OR`` of accessible-database-ids, datasource_access perms,
      catalog_access perms, schema_access perms — matching upstream's
      ``get_dataset_access_filters`` (not just the database-id branch).
    """
    if security_manager.is_admin(user):
        return []
    if await security_manager.can_access_all_datasources(user=user):
        return []

    from sqlalchemy import or_

    from superset.models.connectors import SqlaTable

    accessible_db_ids = await security_manager.get_accessible_database_ids(user) or []
    datasource_perms = await security_manager.user_view_menu_names(
        "datasource_access", user=user
    )
    schema_perms = await security_manager.user_view_menu_names(
        "schema_access", user=user
    )
    catalog_perms = await security_manager.user_view_menu_names(
        "catalog_access", user=user
    )

    return [
        or_(
            SqlaTable.database_id.in_(accessible_db_ids),
            SqlaTable.perm.in_(datasource_perms),
            SqlaTable.catalog_perm.in_(catalog_perms),
            SqlaTable.schema_perm.in_(schema_perms),
        )
    ]


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

    can_access_all = await security_manager.can_access_all_queries(user=user)
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


async def report_access_filters(
    security_manager: Any,
    user: Any,
) -> list[Any]:
    """Return SQLAlchemy filters restricting report schedules to those the user
    can access.

    1:1 port of ``superset_old/reports/filters.py:ReportScheduleFilter``:
    users with the global ``can_access_all_datasources`` permission (which
    includes admins) see every report; everyone else is restricted to the
    reports they own (via the ``ReportSchedule.owners`` M2M).
    """
    can_access_all_datasources = await security_manager.can_access_all_datasources(
        user=user
    )
    if can_access_all_datasources:
        return []

    try:
        from superset.models.reports import ReportSchedule
    except (ImportError, ModuleNotFoundError):
        return []

    user_id = getattr(user, "id", None)
    if user_id is None:
        # No authenticated user — deny everything (mirrors the original which
        # filters on ``get_user_id()`` and would match nothing).
        return [ReportSchedule.id == -1]

    owner_ids_query = (
        select(ReportSchedule.id)
        .join(ReportSchedule.owners)
        .where(ReportSchedule.owners.any(id=user_id))
    )
    return [ReportSchedule.id.in_(owner_ids_query)]


async def saved_query_access_filters(
    security_manager: Any,
    user: Any,
) -> list[Any]:
    """Return SQLAlchemy filters restricting saved queries to the user's own.

    1:1 port of ``superset_old/queries/saved_queries/filters.py:SavedQueryFilter``
    (lines 82-91): the base_filter UNCONDITIONALLY scopes to
    ``SavedQuery.created_by == g.user`` — there is NO bypass for admins or for
    holders of ``can_access_all_queries``.  Even an admin only sees the saved
    queries they created.

    ``created_by`` maps to the ``created_by_fk`` FK column on
    ``AuditMixinNullable``; comparing the FK against the user id is equivalent
    to the original's relationship comparison.
    """
    # ``security_manager`` is intentionally unused — the original filter does
    # not consult any permission and always scopes to the current user.
    del security_manager

    user_id = getattr(user, "id", None)

    try:
        from superset.models.sql_lab import SavedQuery
    except (ImportError, ModuleNotFoundError):
        return []

    if user_id is None:
        # No authenticated user — the original would compare ``created_by``
        # against an anonymous ``g.user`` and match nothing; deny everything.
        return [SavedQuery.created_by_fk == -1]

    return [SavedQuery.created_by_fk == user_id]
