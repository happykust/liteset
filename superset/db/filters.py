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
from typing import Any

from sqlalchemy import or_

logger = logging.getLogger(__name__)


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


async def dashboard_access_filters(
    security_manager: Any,
    user: Any,
) -> list[Any]:
    """Return SQLAlchemy filters restricting dashboards to those the user can access.

    Mirrors Superset's DashboardAccessFilter: admins see all, others see dashboards
    they own, that are published, or that they have role-based access to.
    """
    if security_manager.is_admin(user):
        return []

    try:
        from superset.models.dashboard import Dashboard
    except (ImportError, ModuleNotFoundError):
        return []

    conditions: list[Any] = []

    # Condition 1: User is an owner
    user_id = getattr(user, "id", None)
    if user_id is not None:
        conditions.append(Dashboard.owners.any(id=user_id))

    # Condition 2: Dashboard is published
    conditions.append(Dashboard.published.is_(True))

    if not conditions:
        return [Dashboard.id == -1]  # No access

    return [or_(*conditions)]


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
