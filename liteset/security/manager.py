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
"""Async SecurityManager — full reimplementation of FAB SecurityManager.

Reads from the same ab_* tables as Flask-AppBuilder but via AsyncSession.
Zero database migration needed. Used by AuthMiddleware (short-lived session)
and by controllers/guards (request-scoped session from DI).
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from liteset.exceptions import LitesetSecurityException
from liteset.security.permissions import (
    ALL_DATABASE_ACCESS,
    ALL_DATASOURCE_ACCESS,
    ALL_QUERY_ACCESS,
    CATALOG_ACCESS,
    DATABASE_ACCESS,
    DATASOURCE_ACCESS,
    SCHEMA_ACCESS,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from liteset.security.dao import AsyncSecurityDAO

logger = logging.getLogger(__name__)


class AsyncSecurityManager:
    """Async reimplementation of Superset's SecurityManager.

    Core methods are async equivalents of SupersetSecurityManager:
    - has_access / can_access: permission check
    - raise_for_access: permission check with exception
    - can_access_database / schema / datasource / dashboard
    - is_owner / is_admin
    - get_user_roles / get_schemas_accessible_by_user
    - get_rls_filters
    - guest token create/parse/validate
    - invalidate_user_cache

    All DB queries go through AsyncSecurityDAO.
    """

    def __init__(
        self,
        dao: AsyncSecurityDAO,
        *,
        admin_role_name: str = "Admin",
        public_role_name: str = "Public",
        guest_role_name: str = "Guest",
        dashboard_rbac_enabled: bool = False,
    ) -> None:
        self.dao = dao
        self._admin_role_name = admin_role_name
        self._public_role_name = public_role_name
        self._guest_role_name = guest_role_name
        self._dashboard_rbac_enabled = dashboard_rbac_enabled

    def is_admin(self, user: Any) -> bool:
        """Check if user has the Admin role."""
        roles = getattr(user, "roles", [])
        return any(
            getattr(r, "name", None) == self._admin_role_name for r in roles
        )

    async def has_access(
        self,
        permission_name: str,
        view_name: str,
        *,
        user: Any,
    ) -> bool:
        """Check if user has a specific permission on a view/resource.

        Admin users bypass all permission checks.
        """
        if self.is_admin(user):
            return True
        role_ids = [r.id for r in getattr(user, "roles", [])]
        if not role_ids:
            return False
        return await self.dao.has_permission_view(
            permission_name, view_name, role_ids=role_ids
        )

    async def can_access(
        self,
        permission_name: str,
        view_name: str,
        *,
        user: Any,
    ) -> bool:
        """Alias for has_access (matches Superset API)."""
        return await self.has_access(permission_name, view_name, user=user)

    async def get_user_roles(self, user: Any) -> list[Any]:
        """Get all roles for a user."""
        return await self.dao.get_user_roles(user)

    async def raise_for_access(  # noqa: C901
        self,
        *,
        user: Any,
        database: Any | None = None,
        catalog: str | None = None,
        schema: str | None = None,
        datasource: Any | None = None,
        dashboard: Any | None = None,
        chart: Any | None = None,
        query: Any | None = None,
        query_context: Any | None = None,
    ) -> None:
        """Raise LitesetSecurityException if user lacks access."""
        if self.is_admin(user):
            return

        # Guest users can only access dashboards and their charts.
        # All other resource types (database, datasource, query) are denied.
        if self.is_guest_user(user):
            if dashboard is not None:
                if not await self.has_guest_access(dashboard, user=user):
                    error = self.get_dashboard_access_error_object(dashboard)
                    raise LitesetSecurityException(
                        message=error["message"]
                    )
                return
            if chart is not None:
                chart_dashboard = getattr(chart, "dashboard", None)
                if chart_dashboard and not await self.has_guest_access(
                    chart_dashboard, user=user
                ):
                    raise LitesetSecurityException(
                        message="Guest access denied to chart"
                    )
                return
            # Guest users cannot access databases, datasources, or queries
            raise LitesetSecurityException(
                message="Guest users can only access embedded dashboards"
            )

        if database is not None:
            if not await self.can_access_database(database, user=user):
                raise LitesetSecurityException(
                    message="Access denied to database: "
                    f"{getattr(database, 'perm', '')}"
                )
            if catalog is not None:
                if not await self.can_access_catalog(
                    database, catalog, user=user
                ):
                    raise LitesetSecurityException(
                        message=f"Access denied to catalog: {catalog}"
                    )
                if schema is not None:
                    if not await self.can_access_schema(
                        database, schema, user=user
                    ):
                        raise LitesetSecurityException(
                            message=f"Access denied to schema: {schema}"
                        )
            elif schema is not None:
                if not await self.can_access_schema(
                    database, schema, user=user
                ):
                    raise LitesetSecurityException(
                        message=f"Access denied to schema: {schema}"
                    )
            return

        if datasource is not None:
            if not await self.can_access_datasource(datasource, user=user):
                error = self.get_datasource_access_error_object(datasource)
                raise LitesetSecurityException(
                    message=error["message"]
                )
            return

        if dashboard is not None:
            if not await self.can_access_dashboard(dashboard, user=user):
                error = self.get_dashboard_access_error_object(dashboard)
                raise LitesetSecurityException(
                    message=error["message"]
                )
            return

        if chart is not None:
            if not await self.can_access_chart(chart, user=user):
                raise LitesetSecurityException(
                    message="Access denied to chart"
                )
            return

        if query is not None:
            datasource = getattr(query, "datasource", None)
            if datasource and not await self.can_access_datasource(
                datasource, user=user
            ):
                raise LitesetSecurityException(
                    message="Access denied to query datasource"
                )
            return

        if query_context is not None:
            datasource = getattr(query_context, "datasource", None)
            if datasource and not await self.can_access_datasource(
                datasource, user=user
            ):
                raise LitesetSecurityException(
                    message="Access denied to query context datasource"
                )
            return

    async def can_access_database(
        self, database: Any, *, user: Any
    ) -> bool:
        """Check if user can access a database."""
        if self.is_admin(user):
            return True
        if await self.has_access(
            ALL_DATABASE_ACCESS, ALL_DATABASE_ACCESS, user=user
        ):
            return True
        perm = getattr(database, "perm", None)
        if perm and await self.has_access(DATABASE_ACCESS, perm, user=user):
            return True
        return False

    async def can_access_schema(
        self,
        database: Any,
        schema: str,
        *,
        user: Any,
    ) -> bool:
        """Check if user can access a specific schema."""
        if await self.can_access_database(database, user=user):
            return True
        db_name = getattr(database, "database_name", "")
        schema_perm = f"[{db_name}].[{schema}]"
        return await self.has_access(SCHEMA_ACCESS, schema_perm, user=user)

    async def can_access_datasource(
        self, datasource: Any, *, user: Any
    ) -> bool:
        """Check if user can access a datasource."""
        if self.is_admin(user):
            return True
        if await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        ):
            return True
        perm = getattr(datasource, "perm", None)
        if perm and await self.has_access(DATASOURCE_ACCESS, perm, user=user):
            return True
        database = getattr(datasource, "database", None)
        schema = getattr(datasource, "schema", None)
        if database and schema:
            return await self.can_access_schema(
                database, schema, user=user
            )
        return False

    async def can_access_dashboard(
        self, dashboard: Any, *, user: Any
    ) -> bool:
        """Check if user can access a dashboard."""
        if self.is_admin(user):
            return True

        if self.is_guest_user(user):
            return await self.has_guest_access(dashboard, user=user)

        if self.is_owner(dashboard, user):
            return True

        dashboard_roles = getattr(dashboard, "roles", [])
        if self._dashboard_rbac_enabled and dashboard_roles:
            user_role_ids = {r.id for r in getattr(user, "roles", [])}
            dashboard_role_ids = {r.id for r in dashboard_roles}
            return bool(user_role_ids & dashboard_role_ids)

        if await self.has_access("can_read", "Dashboard", user=user):
            if getattr(dashboard, "published", False):
                return True
            return await self.has_access("can_write", "Dashboard", user=user)
        return False

    def is_owner(self, resource: Any, user: Any) -> bool:
        """Check if user is an owner of the resource."""
        owners = getattr(resource, "owners", [])
        user_id = getattr(user, "id", None)
        if user_id is None:
            return False
        if any(getattr(o, "id", None) == user_id for o in owners):
            return True
        created_by = getattr(resource, "created_by_fk", None)
        return created_by == user_id

    async def get_schemas_accessible_by_user(
        self,
        database: Any,
        schemas: list[str],
        *,
        user: Any,
    ) -> list[str]:
        """Filter schemas to only those accessible by the user."""
        if self.is_admin(user):
            return schemas

        if await self.can_access_database(database, user=user):
            return schemas

        db_name = getattr(database, "database_name", "")
        user_perms = await self.dao.get_all_permissions_for_user(user.id)

        accessible = []
        for schema in schemas:
            schema_perm = f"[{db_name}].[{schema}]"
            if (SCHEMA_ACCESS, schema_perm) in user_perms:
                accessible.append(schema)
        return accessible

    async def get_datasources_accessible_by_user(
        self, *, user: Any
    ) -> list[str]:
        """Get datasource perm strings the user can access.

        Returns perm strings (e.g. "[db].[schema].[table]"), not ORM objects.
        Controllers in liteset/core-api will use these to filter querysets.
        """
        if self.is_admin(user):
            return []  # Admin can access all — empty means no filter
        user_perms = await self.dao.get_all_permissions_for_user(user.id)
        return [
            view_name
            for perm_name, view_name in user_perms
            if perm_name == DATASOURCE_ACCESS
        ]

    async def get_rls_filters(
        self, table: Any, *, user: Any
    ) -> list[Any]:
        """Get Row Level Security filters for a table.

        Stub — always returns []. Full implementation requires
        RowLevelSecurityFilter model queries and clause generation.
        Tracked: TODO(liteset/core-api) — implement RLS filter resolution.
        """
        if self.is_admin(user):
            return []
        return []

    async def invalidate_user_cache(
        self, redis: Redis, user: Any
    ) -> None:
        """Invalidate Redis auth cache for a user."""
        keys = [
            f"auth:user:{user.id}",
            f"auth:user:{user.username}",
            f"auth:user:{user.email}",
        ]
        await redis.delete(*keys)

    # --- Permission string formatters ---

    @staticmethod
    def get_database_perm(database_name: str, database_id: int) -> str:
        """Format database permission string: [db_name].(id:123)."""
        return f"[{database_name}].(id:{database_id})"

    @staticmethod
    def get_schema_perm(database: Any, schema: str) -> str:
        """Format schema permission string: [db_name].[schema_name]."""
        db_name = getattr(database, "database_name", str(database))
        return f"[{db_name}].[{schema}]"

    @staticmethod
    def get_dataset_perm(
        datasource_name: str, schema: str | None, database: str
    ) -> str:
        """Format dataset permission string."""
        if schema:
            return f"[{database}].[{schema}].[{datasource_name}]"
        return f"[{database}]..[{datasource_name}]"

    @staticmethod
    def get_catalog_perm(database_name: str, catalog: str) -> str:
        """Format catalog permission string: [db_name].[catalog]."""
        return f"[{database_name}].[{catalog}]"

    # --- Bulk access checks ---

    async def can_access_all_databases(self, *, user: Any) -> bool:
        """Check if user has the all_database_access permission."""
        return await self.has_access(
            ALL_DATABASE_ACCESS, ALL_DATABASE_ACCESS, user=user
        )

    async def can_access_all_datasources(self, *, user: Any) -> bool:
        """Check if user has the all_datasource_access permission."""
        return await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        )

    async def can_access_all_queries(self, *, user: Any) -> bool:
        """Check if user has the all_query_access permission."""
        return await self.has_access(
            ALL_QUERY_ACCESS, ALL_QUERY_ACCESS, user=user
        )

    # --- List-filtering methods ---

    async def get_accessible_databases(self, *, user: Any) -> list[str]:
        """Get database perm strings the user can access.

        Returns perm strings (e.g. "[db_name].(id:123)"), not ORM objects.
        Controllers in liteset/core-api will use these to filter querysets.
        """
        if self.is_admin(user):
            return []
        user_perms = await self.dao.get_all_permissions_for_user(user.id)
        return [
            view_name
            for perm_name, view_name in user_perms
            if perm_name == DATABASE_ACCESS
        ]

    async def get_catalogs_accessible_by_user(
        self,
        database: Any,
        catalogs: list[str],
        *,
        user: Any,
    ) -> list[str]:
        """Filter catalogs to only those accessible by the user."""
        if self.is_admin(user):
            return catalogs
        if await self.can_access_database(database, user=user):
            return catalogs
        db_name = getattr(database, "database_name", "")
        user_perms = await self.dao.get_all_permissions_for_user(user.id)
        return [
            catalog
            for catalog in catalogs
            if (CATALOG_ACCESS, f"[{db_name}].[{catalog}]") in user_perms
        ]

    async def user_view_menu_names(
        self, permission_name: str, *, user: Any
    ) -> set[str]:
        """Get all view_menu names a user has for a given permission."""
        if self.is_admin(user):
            return set()
        user_perms = await self.dao.get_all_permissions_for_user(user.id)
        return {
            view_name
            for perm_name, view_name in user_perms
            if perm_name == permission_name
        }

    # --- Error object methods ---

    @staticmethod
    def get_datasource_access_error_object(
        datasource: Any,
    ) -> dict[str, Any]:
        """Return a SupersetError-compatible dict for datasource access denial."""
        return {
            "message": (
                f"Access denied to datasource: "
                f"{getattr(datasource, 'perm', '')}"
            ),
            "error_type": "DATASOURCE_SECURITY_ACCESS_ERROR",
            "level": "warning",
            "extra": {
                "link": f"/accessrequest?datasource={getattr(datasource, 'perm', '')}",
                "datasource": getattr(datasource, "perm", ""),
            },
        }

    @staticmethod
    def get_dashboard_access_error_object(
        dashboard: Any,
    ) -> dict[str, Any]:
        """Return a SupersetError-compatible dict for dashboard access denial."""
        return {
            "message": (
                "Access denied to dashboard: "
                f"{getattr(dashboard, 'dashboard_title', '')}"
            ),
            "error_type": "DASHBOARD_SECURITY_ACCESS_ERROR",
            "level": "warning",
            "extra": {
                "link": f"/accessrequest?dashboard_id={getattr(dashboard, 'id', '')}",
                "dashboard_id": getattr(dashboard, "id", ""),
            },
        }

    @staticmethod
    def get_table_access_error_object(
        tables: list[Any],
    ) -> dict[str, Any]:
        """Return a SupersetError-compatible dict for table access denial."""
        table_names = [getattr(t, "perm", str(t)) for t in tables]
        return {
            "message": f"Access denied to tables: {', '.join(table_names)}",
            "error_type": "TABLE_SECURITY_ACCESS_ERROR",
            "level": "warning",
            "extra": {
                "link": "/accessrequest",
                "tables": table_names,
            },
        }

    # --- Ownership checks ---

    def raise_for_ownership(self, resource: Any, *, user: Any) -> None:
        """Raise LitesetSecurityException if user is not owner or admin."""
        if self.is_admin(user):
            return
        if self.is_owner(resource, user):
            return
        raise LitesetSecurityException(
            message="You don't have permission to edit this resource. "
            "Only owners and admins can modify it."
        )

    # --- Guest user checks ---

    def is_guest_user(self, user: Any | None = None) -> bool:
        """Check if the given user is a guest user (JWT-authenticated)."""
        if user is None:
            return False
        return getattr(user, "is_guest", False)

    async def has_guest_access(self, dashboard: Any, *, user: Any) -> bool:
        """Check if a guest user has access to a specific dashboard."""
        if not self.is_guest_user(user):
            return False
        resources = getattr(user, "resources", [])
        dashboard_id = str(getattr(dashboard, "uuid", getattr(dashboard, "id", "")))
        return any(
            r.get("type") == "dashboard" and str(r.get("id")) == dashboard_id
            for r in resources
        )

    # --- Anonymous/Public user ---

    def get_anonymous_user(self) -> Any:
        """Return an AnonymousUser with the PUBLIC role."""
        from liteset.middleware.auth import UnauthenticatedUser

        return UnauthenticatedUser(is_authenticated=False)

    # --- Catalog access ---

    async def can_access_catalog(
        self, database: Any, catalog: str, *, user: Any
    ) -> bool:
        """Check if user can access a specific catalog within a database."""
        if await self.can_access_database(database, user=user):
            return True
        db_name = getattr(database, "database_name", "")
        catalog_perm = f"[{db_name}].[{catalog}]"
        return await self.has_access(CATALOG_ACCESS, catalog_perm, user=user)

    # --- Chart access ---

    async def can_access_chart(self, chart: Any, *, user: Any) -> bool:
        """Check if user can access a chart."""
        if self.is_admin(user):
            return True
        if self.is_owner(chart, user):
            return True
        if not await self.has_access("can_read", "Chart", user=user):
            return False
        datasource = getattr(chart, "datasource", None)
        if datasource:
            return await self.can_access_datasource(datasource, user=user)
        return True

    # --- Guest token management ---

    @staticmethod
    def create_guest_access_token(
        *,
        secret_key: str,
        user: dict[str, Any],
        resources: list[dict[str, Any]],
        rls: list[dict[str, Any]],
        algorithm: str = "HS256",
        exp_seconds: int = 300,
    ) -> str:
        """Create a guest access JWT token.

        Delegates to liteset.security.guest.create_guest_access_token.
        Controllers call this via security_manager.create_guest_access_token().
        """
        from liteset.security.guest import create_guest_access_token

        return create_guest_access_token(
            secret_key=secret_key,
            user=user,
            resources=resources,
            rls=rls,
            exp_seconds=exp_seconds,
        )

    @staticmethod
    def parse_jwt_guest_token(
        token: str,
        secret_key: str,
        algorithm: str = "HS256",
    ) -> dict[str, Any] | None:
        """Parse and validate a guest JWT token.

        Delegates to liteset.security.guest.parse_guest_token.
        """
        from liteset.security.guest import parse_guest_token

        return parse_guest_token(token, secret_key, algorithm=algorithm)

    def get_guest_user_from_request(self, request: Any) -> Any | None:
        """Extract GuestUser from a request if JWT-authenticated.

        Returns the GuestUser from request.user if is_guest is True,
        otherwise None.
        """
        user = getattr(request, "user", None)
        if user is not None and self.is_guest_user(user):
            return user
        return None
