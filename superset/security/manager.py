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

import json
import logging
import re
from typing import Any, cast, TYPE_CHECKING

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import NoResultFound, SQLAlchemyError

from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import SupersetSecurityException
from superset.security.permissions import (
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

    from superset.security.dao import AsyncSecurityDAO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns to extract integer IDs from permission strings
# ---------------------------------------------------------------------------
_DATASOURCE_PERM_RE = re.compile(r"^\[.+\]\.\[.+\]\(id:(?P<id>\d+)\)$")
_DATABASE_PERM_RE = re.compile(r"^\[.+\]\.\(id:(?P<id>\d+)\)$")


# ---------------------------------------------------------------------------
# Query-context modification check (guest user safety)
# ---------------------------------------------------------------------------


def _freeze_value(value: Any) -> str:
    """Deterministic JSON serialization for comparing column/metric sets."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
    return json.dumps(value, sort_keys=True, default=str)


def query_context_modified(query_context: Any) -> bool:
    """Check if a query context has been modified from its stored chart params.

    Used to prevent guest users from altering payloads to fetch data
    different from what was shared with them in dashboards.
    """
    form_data: dict[str, Any] | None = getattr(query_context, "form_data", None)
    stored_chart: Any | None = getattr(query_context, "slice_", None)

    # Native filter requests — no chart to compare against.
    if form_data is None or stored_chart is None:
        return False

    # Cannot request a different chart.
    if form_data.get("slice_id") != stored_chart.id:
        return True

    stored_query_context = (
        json.loads(cast(str, stored_chart.query_context))
        if stored_chart.query_context
        else None
    )

    # Compare columns and metrics in form_data with stored values.
    for key, equivalent in [
        ("metrics", ["metrics"]),
        ("columns", ["columns", "groupby"]),
        ("groupby", ["columns", "groupby"]),
        ("orderby", ["orderby"]),
    ]:
        requested_values = {_freeze_value(value) for value in form_data.get(key) or []}
        stored_values = {
            _freeze_value(value)
            for value in getattr(stored_chart, "params_dict", {}).get(key) or []
        }
        if not requested_values.issubset(stored_values):
            return True

        # Compare queries in query_context.
        queries = getattr(query_context, "queries", [])
        queries_values = {
            _freeze_value(value)
            for query in queries
            for value in getattr(query, key, []) or []
        }
        if stored_query_context:
            for sq in stored_query_context.get("queries") or []:
                for eq_key in equivalent:
                    stored_values.update(
                        {_freeze_value(value) for value in sq.get(eq_key) or []}
                    )

        if not queries_values.issubset(stored_values):
            return True

    return False


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

    _rls_warned: bool = False

    def __init__(
        self,
        dao: AsyncSecurityDAO,
        *,
        admin_role_name: str = "Admin",
        public_role_name: str = "Public",
        guest_role_name: str = "Guest",
        dashboard_rbac_enabled: bool = False,
        embedded_superset_enabled: bool = False,
    ) -> None:
        self.dao = dao
        self._admin_role_name = admin_role_name
        self._public_role_name = public_role_name
        self._guest_role_name = guest_role_name
        self._dashboard_rbac_enabled = dashboard_rbac_enabled
        self._embedded_superset_enabled = embedded_superset_enabled

    @staticmethod
    def get_exclude_users_from_lists() -> list[str]:
        """Override to dynamically identify usernames to exclude from
        all UI dropdown lists (owners, created_by filters, etc.).

        Mirrors the original ``SupersetSecurityManager.get_exclude_users_from_lists``
        which is called as a fallback when ``EXCLUDE_USERS_FROM_LISTS`` config is None.

        :return: A list of usernames to exclude
        """
        return []

    async def find_user_by_id(self, user_id: int) -> Any | None:
        """Find a user by primary key (ab_user table)."""
        return await self.dao.get_user_by_id(user_id)

    async def find_role_by_id(self, role_id: int) -> Any | None:
        """Find a role by primary key (ab_role table)."""
        role_model: Any = self.dao.role_model
        stmt: Any = select(role_model).where(role_model.id == role_id)
        result = await self.dao.session.execute(stmt)
        return result.scalars().one_or_none()

    def is_admin(self, user: Any) -> bool:
        """Check if user has the Admin role."""
        roles = getattr(user, "roles", [])
        return any(getattr(r, "name", None) == self._admin_role_name for r in roles)

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
        # Fast path: check pre-resolved permissions (CachedUser, GuestUser)
        user_perms = getattr(user, "permissions", None)
        if isinstance(user_perms, (set, frozenset)):
            return (permission_name, view_name) in user_perms
        # Slow path: DAO query for ORM users
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

    async def raise_for_access(  # noqa: C901, PLR0912, PLR0915
        self,
        *,
        user: Any,
        database: Any | None = None,
        catalog: str | None = None,
        schema: str | None = None,
        table: Any | None = None,
        datasource: Any | None = None,
        dashboard: Any | None = None,
        chart: Any | None = None,
        query: Any | None = None,
        query_context: Any | None = None,
        viz: Any | None = None,
        sql: str | None = None,
        template_params: dict[str, Any] | None = None,
    ) -> None:
        """Raise SupersetSecurityException if user lacks access.

        Mirrors the original ``SupersetSecurityManager.raise_for_access``
        with the same ordering:
        1. sql + database -> synthetic Query creation
        2. table/query path (database -> catalog -> schema -> datasource)
        3. Guest query_context modification check
        4. datasource/query_context/viz path (with dashboard RBAC fallback)
        5. dashboard path
        6. chart path

        :param database: The Superset database
        :param datasource: The Superset datasource
        :param query: The SQL Lab query
        :param query_context: The query context
        :param table: The Superset table (requires database)
        :param viz: The visualization
        :param sql: The SQL string (requires database)
        :param catalog: Optional catalog name
        :param schema: Optional schema name
        :param template_params: Optional template parameters for Jinja templating
        :raises SupersetSecurityException: If the user cannot access the resource
        """
        if self.is_admin(user):
            return

        # ------------------------------------------------------------------
        # Synthetic Query from raw SQL  (original lines 2315-2324)
        # ------------------------------------------------------------------
        if sql and database:
            from superset.models.sql_lab import Query as QueryModel
            from superset.utils.core import shortid

            query = QueryModel(
                database=database,
                sql=sql,
                schema=schema,
                catalog=catalog,
                client_id=shortid()[:10],
                user_id=getattr(user, "id", None),
            )
            # Expunge from session so it's not persisted — mirrors
            # ``self.session.expunge(query)`` in the original.
            try:
                from sqlalchemy import inspect as sa_inspect

                state = sa_inspect(query, raiseerr=False)
                if state is not None and state.session is not None:
                    state.session.expunge(query)
            except Exception:  # noqa: BLE001, S110
                pass  # Query may not be in a session — safe to ignore

        # ------------------------------------------------------------------
        # Path 1: database + table  OR  query
        # Mirrors original lines 2326-2397
        # ------------------------------------------------------------------
        if (database and table) or query:
            if query:
                database = getattr(query, "database", database)

            database = cast(Any, database)
            default_catalog = (
                database.db_engine_spec.get_default_catalog(database)
                if hasattr(database, "db_engine_spec")
                else None
            )

            if await self.can_access_database(database, user=user):
                return

            tables: set[Any] = set()
            if query:
                # Extract all referenced tables from the SQL via Jinja
                # rendering + SQLGlot parsing.
                # Mirrors original lines 2336-2355.
                default_schema = self._get_default_schema_for_query(
                    database, query, template_params
                )
                try:
                    from superset.sql.parse import process_jinja_sql

                    jinja_result = process_jinja_sql(
                        query.sql, database, template_params
                    )
                    tables = {
                        table_.qualify(
                            catalog=getattr(query, "catalog", None) or default_catalog,
                            schema=default_schema,
                        )
                        for table_ in jinja_result.tables
                    }
                except Exception:  # noqa: BLE001
                    # If Jinja/SQLGlot parsing fails, fall back to
                    # direct SQL parsing without Jinja rendering.
                    logger.warning(
                        "Failed to process Jinja SQL, falling back to direct parsing",
                        exc_info=True,
                    )
                    try:
                        from superset.sql.parse import SQLScript

                        engine = (
                            database.db_engine_spec.engine
                            if hasattr(database, "db_engine_spec")
                            else "base"
                        )
                        parsed = SQLScript(query.sql, engine=engine)
                        tables = set()
                        for stmt in parsed.statements:
                            tables |= {
                                t.qualify(
                                    catalog=(
                                        getattr(query, "catalog", None)
                                        or default_catalog
                                    ),
                                    schema=default_schema,
                                )
                                for t in stmt.tables
                            }
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "Failed to parse SQL for table extraction",
                            exc_info=True,
                        )

            elif table:
                # Make sure table has the default catalog, if not specified.
                if hasattr(table, "qualify"):
                    table = table.qualify(catalog=default_catalog)
                tables = {table}

            denied: set[Any] = set()
            for table_ in tables:
                # Catalog-level check
                catalog_perm = self.get_catalog_perm(
                    getattr(database, "database_name", ""),
                    getattr(table_, "catalog", None) or "",
                )
                if catalog_perm and await self.can_access(
                    CATALOG_ACCESS, catalog_perm, user=user
                ):
                    continue

                # Schema-level check
                schema_perm = self.get_schema_perm(
                    database,
                    getattr(table_, "schema", None) or "",
                    catalog=getattr(table_, "catalog", None),
                )
                if schema_perm and await self.can_access(
                    SCHEMA_ACCESS, schema_perm, user=user
                ):
                    continue

                # Datasource-level check + ownership
                table_name = getattr(table_, "table", None) or str(table_)
                if await self._can_access_table_datasource(
                    database,
                    table_name,
                    getattr(table_, "schema", None),
                    getattr(table_, "catalog", None),
                    user=user,
                ):
                    continue

                denied.add(table_)

            if denied:
                raise SupersetSecurityException(
                    self.get_table_access_error_object(denied)
                )

        # ------------------------------------------------------------------
        # Path 2: Guest user query_context modification check
        # Mirrors original lines 2399-2412.
        # MUST come between table/query and datasource/query_context/viz.
        # ------------------------------------------------------------------
        if (
            query_context
            and self.is_guest_user(user)
            and query_context_modified(query_context)
        ):
            raise SupersetSecurityException(
                SupersetError(
                    error_type=SupersetErrorType.DASHBOARD_SECURITY_ACCESS_ERROR,
                    message="Guest user cannot modify chart payload",
                    level=ErrorLevel.WARNING,
                )
            )

        # ------------------------------------------------------------------
        # Path 3: datasource / query_context / viz
        # Mirrors original lines 2414-2485 — includes dashboard RBAC fallback.
        # ------------------------------------------------------------------
        if datasource or query_context or viz:
            form_data: dict[str, Any] | None = None

            if query_context:
                datasource = getattr(query_context, "datasource", datasource)
                form_data = getattr(query_context, "form_data", None)
            elif viz:
                datasource = getattr(viz, "datasource", datasource)
                form_data = getattr(viz, "form_data", None)

            assert datasource

            # Check direct access first, then dashboard RBAC fallback
            has_direct_access = (
                await self._can_access_datasource_schema(datasource, user=user)
                or await self.can_access(
                    DATASOURCE_ACCESS,
                    getattr(datasource, "perm", "") or "",
                    user=user,
                )
                or self.is_owner(datasource, user)
            )

            if not has_direct_access:
                # Dashboard RBAC fallback: when user lacks direct datasource
                # access but has access to a dashboard using it (via
                # form_data.dashboardId), access is granted.
                # Mirrors original lines 2435-2481.
                dashboard_fallback = False
                if form_data and (dashboard_id := form_data.get("dashboardId")):
                    dashboard_ = await self._get_dashboard_by_id(dashboard_id)
                    if dashboard_ is not None:
                        # Check if dashboard RBAC or embedded guest applies
                        rbac_or_guest = (
                            self._dashboard_rbac_enabled
                            and getattr(dashboard_, "roles", [])
                        ) or (
                            self._embedded_superset_enabled and self.is_guest_user(user)
                        )

                        if rbac_or_guest:
                            # Validate the specific resource (native filter,
                            # chart, or drill-by)
                            resource_valid = False

                            if form_data.get("type") == "NATIVE_FILTER":
                                # Native filter validation
                                native_filter_id = form_data.get("native_filter_id")
                                json_metadata_raw = getattr(
                                    dashboard_, "json_metadata", None
                                )
                                if native_filter_id and json_metadata_raw:
                                    try:
                                        json_metadata = json.loads(json_metadata_raw)
                                    except (json.JSONDecodeError, TypeError):
                                        json_metadata = {}
                                    resource_valid = any(
                                        target.get("datasetId") == datasource.id
                                        for fltr in json_metadata.get(
                                            "native_filter_configuration", []
                                        )
                                        for target in fltr.get("targets", [])
                                        if native_filter_id == fltr.get("id")
                                    )
                            else:
                                slice_id = form_data.get("slice_id")
                                if slice_id:
                                    # Chart-in-dashboard validation
                                    slc = await self._get_slice_by_id(slice_id)
                                    if (
                                        slc is not None
                                        and slc in getattr(dashboard_, "slices", [])
                                        and getattr(slc, "datasource", None)
                                        == datasource
                                    ):
                                        resource_valid = True

                                # Drill-by access check
                                if not resource_valid:
                                    resource_valid = await self._has_drill_by_access(
                                        form_data, dashboard_, datasource
                                    )

                            # Finally check dashboard-level access
                            if resource_valid and await self.can_access_dashboard(
                                dashboard_, user=user
                            ):
                                dashboard_fallback = True

                if not dashboard_fallback:
                    raise SupersetSecurityException(
                        self.get_datasource_access_error_object(datasource)
                    )

        # ------------------------------------------------------------------
        # Path 4: dashboard
        # Mirrors original lines 2487-2527.
        # ------------------------------------------------------------------
        if dashboard:
            if self.is_guest_user(user):
                # Guest user is currently used for embedded dashboards only.
                if await self.has_guest_access(dashboard, user=user):
                    return
                raise SupersetSecurityException(
                    self.get_dashboard_access_error_object(dashboard)
                )

            if self.is_admin(user) or self.is_owner(dashboard, user):
                return

            # DASHBOARD_RBAC logic
            if self._dashboard_rbac_enabled and getattr(dashboard, "roles", []):
                if getattr(dashboard, "published", False) and {
                    role.id for role in getattr(dashboard, "roles", [])
                } & {role.id for role in getattr(user, "roles", [])}:
                    return

            # REGULAR RBAC logic
            else:
                datasources = getattr(dashboard, "datasources", None)
                if not datasources:
                    return
                for ds in datasources:
                    if await self.can_access_datasource(ds, user=user):
                        return

            raise SupersetSecurityException(
                self.get_dashboard_access_error_object(dashboard)
            )

        # ------------------------------------------------------------------
        # Path 5: chart
        # Mirrors original lines 2529-2536.
        # ------------------------------------------------------------------
        if chart:
            if self.is_admin(user) or self.is_owner(chart, user):
                return

            chart_ds = getattr(chart, "datasource", None)
            if chart_ds and await self.can_access_datasource(chart_ds, user=user):
                return

            raise SupersetSecurityException(
                self.get_chart_access_error_object(chart)
            )

    @staticmethod
    def _get_default_schema_for_query(
        database: Any,
        query: Any,
        template_params: dict[str, Any] | None = None,
    ) -> str | None:
        """Return the default schema for a given query.

        Mirrors ``Database.get_default_schema_for_query`` from the original
        which delegates to ``db_engine_spec.get_default_schema_for_query``.

        Since the liteset Database model may not have this method, we
        replicate the logic from ``BaseEngineSpec.get_default_schema_for_query``:

        1. If the engine spec supports dynamic schemas, use the query schema.
        2. Otherwise check if the schema is in the SQLAlchemy URI / connect_args.
        3. Fall back to ``get_default_schema(database, query.catalog)``.
        """
        if not hasattr(database, "db_engine_spec"):
            return getattr(query, "schema", None)

        spec = database.db_engine_spec

        # Original: Database.get_default_schema_for_query delegates to engine spec
        if hasattr(spec, "get_default_schema_for_query"):
            return spec.get_default_schema_for_query(database, query, template_params)

        # Inline the BaseEngineSpec.get_default_schema_for_query logic
        if getattr(spec, "supports_dynamic_schema", False):
            return getattr(query, "schema", None)

        # Check if schema is stored in SQLAlchemy URI or connect_args
        try:
            connect_args = database.get_extra()["engine_params"]["connect_args"]
        except (KeyError, TypeError):
            connect_args = {}

        if hasattr(spec, "get_schema_from_engine_params"):
            from sqlalchemy.engine import make_url as make_url_safe

            sqlalchemy_uri = make_url_safe(database.sqlalchemy_uri)
            schema_from_params = spec.get_schema_from_engine_params(
                sqlalchemy_uri, connect_args
            )
            if schema_from_params:
                return schema_from_params

        # Fall back to default schema for the catalog
        if hasattr(spec, "get_default_schema"):
            return spec.get_default_schema(database, getattr(query, "catalog", None))

        return getattr(query, "schema", None)

    async def _can_access_datasource_schema(
        self, datasource: Any, *, user: Any
    ) -> bool:
        """Check schema-level access for a datasource.

        Mirrors the original ``can_access_schema(datasource)`` which takes
        a datasource and checks all_datasource_access, database, catalog,
        and schema_perm.
        """
        if await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        ):
            return True
        database = getattr(datasource, "database", None)
        if database and await self.can_access_database(database, user=user):
            return True
        ds_catalog = getattr(datasource, "catalog", None)
        if ds_catalog and database:
            if await self.can_access_catalog(database, ds_catalog, user=user):
                return True
        schema_perm = getattr(datasource, "schema_perm", None)
        if schema_perm and await self.can_access(SCHEMA_ACCESS, schema_perm, user=user):
            return True
        return False

    async def _get_dashboard_by_id(self, dashboard_id: Any) -> Any | None:
        """Load a Dashboard by ID for dashboard RBAC fallback.

        Mirrors the original ``self.session.query(Dashboard)
        .filter(Dashboard.id == dashboard_id).one_or_none()``.
        """
        from superset.models.dashboard import Dashboard

        stmt = select(Dashboard).where(Dashboard.id == dashboard_id)
        result = await self.dao.session.execute(stmt)
        return result.scalars().one_or_none()

    async def _get_slice_by_id(self, slice_id: Any) -> Any | None:
        """Load a Slice by ID for chart-in-dashboard validation."""
        from superset.models.slice import Slice

        stmt = select(Slice).where(Slice.id == slice_id)
        result = await self.dao.session.execute(stmt)
        return result.scalars().one_or_none()

    async def _has_drill_by_access(
        self,
        form_data: dict[str, Any],
        dashboard: Any,
        datasource: Any,
    ) -> bool:
        """Check if form_data is performing a supported drill-by operation.

        Mirrors the original ``has_drill_by_access`` exactly:
        - type != NATIVE_FILTER
        - slice_id == 0
        - chart_id must reference a chart in the dashboard
        - chart datasource must match
        - requested dimensions must be a subset of drillable columns
        """
        from superset.models.connectors import TableColumn
        from superset.models.slice import Slice

        if form_data.get("type") == "NATIVE_FILTER":
            return False
        if form_data.get("slice_id") != 0:
            return False
        chart_id = form_data.get("chart_id")
        if not chart_id:
            return False

        # Load the chart
        stmt = select(Slice).where(Slice.id == chart_id)
        result = await self.dao.session.execute(stmt)
        slc = result.scalars().one_or_none()
        if slc is None:
            return False
        if slc not in getattr(dashboard, "slices", []):
            return False
        if getattr(slc, "datasource", None) != datasource:
            return False

        dimensions = form_data.get("groupby")
        if not dimensions:
            return False

        # Load drillable columns
        stmt_cols = (
            select(TableColumn.column_name)
            .where(TableColumn.table_id == datasource.id)
            .where(TableColumn.groupby.is_(True))
        )
        result_cols = await self.dao.session.execute(stmt_cols)
        drillable_columns = {row[0] for row in result_cols.all()}
        if not drillable_columns:
            return False

        return set(dimensions).issubset(drillable_columns)

    async def can_access_database(self, database: Any, *, user: Any) -> bool:
        """Check if user can access a database."""
        if self.is_admin(user):
            return True
        if await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        ):
            return True
        if await self.has_access(ALL_DATABASE_ACCESS, ALL_DATABASE_ACCESS, user=user):
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
        catalog: str | None = None,
        user: Any,
    ) -> bool:
        """Check if user can access a specific schema.

        Mirrors the original ``can_access_schema(datasource)`` hierarchy:
        all_datasource_access -> database_access -> catalog_access -> schema_access.

        For catalog-aware databases (e.g. ClickHouse, Trino), pass the
        ``catalog`` parameter to build the 3-part permission string
        ``[db].[catalog].[schema]``.  Without a catalog the traditional
        2-part ``[db].[schema]`` is used.
        """
        if await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        ):
            return True
        if await self.can_access_database(database, user=user):
            return True
        # Catalog-level check — mirrors original line 555-557
        if catalog:
            if await self.can_access_catalog(database, catalog, user=user):
                return True
        db_name = getattr(database, "database_name", "")
        if catalog:
            schema_perm = f"[{db_name}].[{catalog}].[{schema}]"
        else:
            schema_perm = f"[{db_name}].[{schema}]"
        return await self.has_access(SCHEMA_ACCESS, schema_perm, user=user)

    async def can_access_table(
        self,
        database: Any,
        table: Any,
        *,
        user: Any,
    ) -> bool:
        """Check if user can access a specific table.

        Mirrors ``SupersetSecurityManager.can_access_table`` from the
        original FAB-based security manager.

        :param database: The Database model instance
        :param table: A ``Table`` instance with ``.table``, ``.schema``,
            ``.catalog`` attributes
        :param user: The current user
        :returns: Whether the user can access the table
        """
        try:
            await self.raise_for_access(database=database, table=table, user=user)
        except SupersetSecurityException:
            return False
        return True

    async def _can_access_table_datasource(
        self,
        database: Any,
        table_name: str,
        schema: str | None,
        catalog: str | None,
        *,
        user: Any,
    ) -> bool:
        """Check datasource-level access for a specific table.

        Looks up SqlaTable rows matching the given table name and checks
        if the user has ``datasource_access`` or is owner of any matching
        datasource.  Mirrors the original FAB table-level access check
        where individual datasource permissions are checked after
        database/catalog/schema checks fail.
        """
        try:
            from superset.models.connectors import SqlaTable

            session = self.dao.session
            stmt = select(SqlaTable).where(
                SqlaTable.table_name == table_name,
                SqlaTable.database_id == database.id,
            )
            if schema is not None:
                stmt = stmt.where(SqlaTable.schema == schema)
            if catalog is not None and hasattr(SqlaTable, "catalog"):
                stmt = stmt.where(SqlaTable.catalog == catalog)

            result = await session.execute(stmt)
            datasources = result.scalars().all()

            for ds in datasources:
                if await self.can_access_datasource(ds, user=user):
                    return True
                if self.is_owner(ds, user):
                    return True
        except (SQLAlchemyError, NoResultFound):
            logger.warning(
                "Failed to check table datasource access for %s.%s",
                schema,
                table_name,
                exc_info=True,
            )
        return False

    async def can_access_datasource(self, datasource: Any, *, user: Any) -> bool:
        """Check if user can access a datasource.

        Note: The original ``can_access_datasource`` delegates to
        ``raise_for_access(datasource=datasource)`` which checks
        schema access, datasource_access perm, AND ownership.
        We inline those checks here rather than recursing into
        raise_for_access to avoid the dashboard RBAC fallback path.
        """
        if self.is_admin(user):
            return True
        if await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        ):
            return True
        perm = getattr(datasource, "perm", None)
        if perm and await self.has_access(DATASOURCE_ACCESS, perm, user=user):
            return True
        # Ownership check — mirrors original raise_for_access line 2429
        if self.is_owner(datasource, user):
            return True
        # Schema-level check (includes database, catalog, schema_perm)
        if await self._can_access_datasource_schema(datasource, user=user):
            return True
        return False

    async def can_access_dashboard(self, dashboard: Any, *, user: Any) -> bool:  # noqa: C901
        """Check if user can access a dashboard."""
        if self.is_admin(user):
            return True

        if self.is_guest_user(user):
            return await self.has_guest_access(dashboard, user=user)

        if self.is_owner(dashboard, user):
            return True

        dashboard_roles = getattr(dashboard, "roles", [])
        if self._dashboard_rbac_enabled and dashboard_roles:
            if not getattr(dashboard, "published", False):
                return False
            user_role_ids = {r.id for r in getattr(user, "roles", [])}
            dashboard_role_ids = {r.id for r in dashboard_roles}
            return bool(user_role_ids & dashboard_role_ids)

        # Non-RBAC: check datasource-based access
        # Prefer dashboard.datasources (M2M property) over iterating slices
        datasources = getattr(dashboard, "datasources", None)
        if datasources is not None:
            if not datasources:
                return True  # Empty dashboard is accessible to all authenticated users
            for ds in datasources:
                if await self.can_access_datasource(ds, user=user):
                    return True
            return False
        # Fallback: iterate slices
        slices = getattr(dashboard, "slices", [])
        if not slices:
            return True
        for slc in slices:
            datasource = getattr(slc, "datasource", None)
            if datasource and await self.can_access_datasource(datasource, user=user):
                return True
        return False

    def is_owner(self, resource: Any, user: Any) -> bool:
        """Check if user is an owner of the resource (owners M2M only)."""
        user_id: int | None
        if isinstance(user, int):
            user_id = user
        else:
            user_id = getattr(user, "id", None)
        if user_id is None:
            return False
        owners = getattr(resource, "owners", [])
        return any(getattr(o, "id", None) == user_id for o in owners)

    async def get_schemas_accessible_by_user(
        self,
        database: Any,
        schemas: list[str],
        *,
        catalog: str | None = None,
        user: Any,
    ) -> list[str]:
        """Filter schemas to only those accessible by the user."""
        if self.is_admin(user):
            return schemas

        if await self.can_access_database(database, user=user):
            return schemas

        db_name = getattr(database, "database_name", "")
        user_perms = await self.dao.get_all_permissions_for_user_with_groups(user.id)

        accessible = []
        for schema in schemas:
            if catalog:
                schema_perm = f"[{db_name}].[{catalog}].[{schema}]"
            else:
                schema_perm = f"[{db_name}].[{schema}]"
            if (SCHEMA_ACCESS, schema_perm) in user_perms:
                accessible.append(schema)
        return accessible

    async def get_datasources_accessible_by_user(self, *, user: Any) -> list[str]:
        """Get datasource perm strings the user can access.

        Returns perm strings (e.g. "[db].[schema].[table]"), not ORM objects.
        Controllers in superset/core-api will use these to filter querysets.
        """
        if self.is_admin(user):
            return []  # Admin can access all — empty means no filter
        user_perms = await self.dao.get_all_permissions_for_user_with_groups(user.id)
        return [
            view_name
            for perm_name, view_name in user_perms
            if perm_name == DATASOURCE_ACCESS
        ]

    async def get_rls_filters(self, table: Any, *, user: Any) -> list[Any]:
        """Get Row Level Security filters for a table.

        Queries RowLevelSecurityFilter with M2M joins on tables and roles.
        Two filter types:
        - Regular: user HAS the role -> filter applies
        - Base: user does NOT have the role -> filter applies
        Results are ordered by group_key.
        """
        from superset.models.connectors import (
            RLSFilterRoles,
            RLSFilterTables,
            RowLevelSecurityFilter,
        )

        if self.is_admin(user):
            return []

        user_roles = [r.id for r in getattr(user, "roles", [])]

        # Sub-query: RLS filter IDs that apply to this table
        filter_tables_sq = select(RLSFilterTables.c.rls_filter_id).where(
            RLSFilterTables.c.table_id == table.id
        )

        # Sub-query: Regular filters where user has the role
        regular_filter_roles_sq = (
            select(RLSFilterRoles.c.rls_filter_id)
            .join(
                RowLevelSecurityFilter,
                RLSFilterRoles.c.rls_filter_id == RowLevelSecurityFilter.id,
            )
            .where(RowLevelSecurityFilter.filter_type == "Regular")
            .where(RLSFilterRoles.c.role_id.in_(user_roles))
        )

        # Sub-query: Base filters where user has the role (to be excluded)
        base_filter_roles_sq = (
            select(RLSFilterRoles.c.rls_filter_id)
            .join(
                RowLevelSecurityFilter,
                RLSFilterRoles.c.rls_filter_id == RowLevelSecurityFilter.id,
            )
            .where(RowLevelSecurityFilter.filter_type == "Base")
            .where(RLSFilterRoles.c.role_id.in_(user_roles))
        )

        stmt = (
            select(RowLevelSecurityFilter)
            .where(RowLevelSecurityFilter.id.in_(filter_tables_sq))
            .where(
                or_(
                    and_(
                        RowLevelSecurityFilter.filter_type == "Regular",
                        RowLevelSecurityFilter.id.in_(regular_filter_roles_sq),
                    ),
                    and_(
                        RowLevelSecurityFilter.filter_type == "Base",
                        RowLevelSecurityFilter.id.notin_(base_filter_roles_sq),
                    ),
                )
            )
            .order_by(RowLevelSecurityFilter.group_key)
        )

        result = await self.dao.session.execute(stmt)
        return list(result.scalars().all())

    async def get_rls_sorted(self, table: Any, *, user: Any) -> list[Any]:
        """Retrieve RLS filters sorted by ID for deterministic cache keys.

        :param table: The datasource/table to check against.
        :param user: The current user.
        :returns: A list of RowLevelSecurityFilter objects sorted by ID.
        """
        filters = await self.get_rls_filters(table, user=user)
        filters.sort(key=lambda f: f.id)
        return filters

    def get_guest_rls_filters(self, dataset: Any, *, user: Any) -> list[dict[str, Any]]:
        """Retrieve RLS filters from a guest token for the given dataset.

        Matches the original SupersetSecurityManager.get_guest_rls_filters:
        returns rules from the guest token that either have no dataset
        restriction or match the given dataset's ID.

        :param dataset: The datasource to check against.
        :param user: The current user (may be a GuestUser with rls_rules).
        :returns: A list of RLS rule dicts from the guest token.
        """
        if not self.is_guest_user(user):
            return []
        rls_rules: list[dict[str, Any]] = getattr(user, "rls_rules", [])
        return [
            rule
            for rule in rls_rules
            if not rule.get("dataset") or str(rule.get("dataset")) == str(dataset.id)
        ]

    def get_guest_rls_filters_str(self, table: Any, *, user: Any) -> list[str]:
        """Return guest RLS filter clauses as strings.

        :param table: The datasource to check against.
        :param user: The current user.
        :returns: A list of clause strings from guest token RLS rules.
        """
        return [
            f.get("clause", "") for f in self.get_guest_rls_filters(table, user=user)
        ]

    async def get_rls_cache_key(self, datasource: Any, *, user: Any) -> list[str]:
        """Return cache key components representing active RLS filters.

        Combines both regular RLS filters (from DB, sorted by ID) and
        guest token RLS filters to build a deterministic list of strings
        for cache differentiation. This matches the original
        SupersetSecurityManager.get_rls_cache_key exactly.
        """
        rls_clauses_with_group_key: list[str] = []
        if getattr(datasource, "is_rls_supported", False):
            rls_clauses_with_group_key = [
                f"{f.clause}-{f.group_key or ''}"
                for f in await self.get_rls_sorted(datasource, user=user)
            ]
        guest_rls = self.get_guest_rls_filters_str(datasource, user=user)
        return guest_rls + rls_clauses_with_group_key

    async def invalidate_user_cache(self, redis: Redis, user: Any) -> None:
        """Invalidate Redis auth cache for a user.

        Deletes all possible cache keys: by id, username, and email.
        This ensures cache is fully cleared regardless of which key
        was used to store the cached user data.
        """
        keys = [f"auth:user:{user.id}"]
        username = getattr(user, "username", None)
        if username:
            keys.append(f"auth:user:{username}")
        email = getattr(user, "email", None)
        if email:
            keys.append(f"auth:user:{email}")
        await redis.delete(*keys)

    # --- Permission string formatters ---

    @staticmethod
    def get_database_perm(database_name: str, database_id: int) -> str:
        """Format database permission string: [db_name].(id:123)."""
        return f"[{database_name}].(id:{database_id})"

    @staticmethod
    def get_schema_perm(database: Any, schema: str, catalog: str | None = None) -> str:
        """Format schema permission string.

        Returns ``[db].[catalog].[schema]`` or ``[db].[schema]``.
        """
        db_name = getattr(database, "database_name", str(database))
        if catalog:
            return f"[{db_name}].[{catalog}].[{schema}]"
        return f"[{db_name}].[{schema}]"

    @staticmethod
    def get_dataset_perm(database_name: str, dataset_name: str, dataset_id: int) -> str:
        """Format dataset permission string: [db_name].[dataset_name](id:N)."""
        return f"[{database_name}].[{dataset_name}](id:{dataset_id})"

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
        return await self.has_access(ALL_QUERY_ACCESS, ALL_QUERY_ACCESS, user=user)

    # --- List-filtering methods (ID-based, for object-level filters) ---

    async def get_accessible_datasource_ids(self, user: Any) -> list[int]:
        """Return list of datasource IDs the user can access.

        Admins get an empty list (meaning no filter — access everything).
        For other users, parses DATASOURCE_ACCESS permission strings using
        the ``[db].[table](id:N)`` regex to extract integer IDs.
        """
        if self.is_admin(user):
            return []
        user_perms = await self.dao.get_all_permissions_for_user_with_groups(user.id)
        ids: list[int] = []
        for perm_name, view_name in user_perms:
            if perm_name != DATASOURCE_ACCESS:
                continue
            m = _DATASOURCE_PERM_RE.match(view_name)
            if m:
                ids.append(int(m.group("id")))
        return ids

    async def get_accessible_database_ids(self, user: Any) -> list[int]:
        """Return list of database IDs the user can access.

        Admins get an empty list (meaning no filter — access everything).
        For other users, parses DATABASE_ACCESS permission strings using
        the ``[db].(id:N)`` regex to extract integer IDs.
        """
        if self.is_admin(user):
            return []
        user_perms = await self.dao.get_all_permissions_for_user_with_groups(user.id)
        ids: list[int] = []
        for perm_name, view_name in user_perms:
            if perm_name != DATABASE_ACCESS:
                continue
            m = _DATABASE_PERM_RE.match(view_name)
            if m:
                ids.append(int(m.group("id")))
        return ids

    # --- List-filtering methods (perm-string-based) ---

    async def get_accessible_databases(self, *, user: Any) -> list[str]:
        """Get database perm strings the user can access.

        Returns perm strings (e.g. "[db_name].(id:123)"), not ORM objects.
        Controllers in superset/core-api will use these to filter querysets.
        """
        if self.is_admin(user):
            return []
        user_perms = await self.dao.get_all_permissions_for_user_with_groups(user.id)
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
        user_perms = await self.dao.get_all_permissions_for_user_with_groups(user.id)
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
        user_perms = await self.dao.get_all_permissions_for_user_with_groups(user.id)
        return {
            view_name
            for perm_name, view_name in user_perms
            if perm_name == permission_name
        }

    # --- Error object methods ---
    # These return SupersetError dataclasses, matching the original 1:1.

    @staticmethod
    def get_datasource_access_error_msg(datasource: Any) -> str:
        """Return the error message for the denied datasource."""
        ds_id = getattr(datasource, "id", "")
        return (
            f"This endpoint requires the datasource {ds_id}, "
            "database or `all_datasource_access` permission"
        )

    @staticmethod
    def get_datasource_access_link(datasource: Any) -> str | None:
        """Return the link for the denied datasource."""
        return None

    def get_datasource_access_error_object(
        self,
        datasource: Any,
    ) -> SupersetError:
        """Return the SupersetError for the denied datasource."""
        return SupersetError(
            error_type=SupersetErrorType.DATASOURCE_SECURITY_ACCESS_ERROR,
            message=self.get_datasource_access_error_msg(datasource),
            level=ErrorLevel.WARNING,
            extra={
                "link": self.get_datasource_access_link(datasource),
                "datasource": getattr(datasource, "id", ""),
                "datasource_name": getattr(datasource, "name", ""),
            },
        )

    @staticmethod
    def get_dashboard_access_error_object(
        dashboard: Any,
    ) -> SupersetError:
        """Return the SupersetError for the denied dashboard."""
        return SupersetError(
            error_type=SupersetErrorType.DASHBOARD_SECURITY_ACCESS_ERROR,
            message="You don't have access to this dashboard.",
            level=ErrorLevel.WARNING,
        )

    @staticmethod
    def get_chart_access_error_object(
        chart: Any,
    ) -> SupersetError:
        """Return the SupersetError for the denied chart."""
        return SupersetError(
            error_type=SupersetErrorType.CHART_SECURITY_ACCESS_ERROR,
            message="You don't have access to this chart.",
            level=ErrorLevel.WARNING,
        )

    def get_table_access_error_msg(self, tables: set[Any]) -> str:
        """Return the error message for the denied SQL tables."""
        quoted_tables = [f"`{table}`" for table in tables]
        return (
            f"You need access to the following tables: {', '.join(quoted_tables)},\n"
            "            `all_database_access` or `all_datasource_access` permission"
        )

    @staticmethod
    def get_table_access_link(tables: set[Any]) -> str | None:
        """Return the access link for the denied SQL tables."""
        return None

    def get_table_access_error_object(
        self,
        tables: set[Any],
    ) -> SupersetError:
        """Return the SupersetError for the denied SQL tables."""
        return SupersetError(
            error_type=SupersetErrorType.TABLE_SECURITY_ACCESS_ERROR,
            message=self.get_table_access_error_msg(tables),
            level=ErrorLevel.WARNING,
            extra={
                "link": self.get_table_access_link(tables),
                "tables": [str(table) for table in tables],
            },
        )

    # --- Ownership checks ---

    async def raise_for_ownership(
        self,
        resource: Any,
        user_id: int | None,
    ) -> None:
        """Raise SupersetSecurityException if user is not owner and not admin.

        Admin users bypass the ownership check entirely, mirroring
        Superset's ``raise_for_ownership()`` behaviour.
        """
        if user_id is None:
            raise SupersetSecurityException(
                message="Authentication required to modify this resource."
            )
        # Fetch user to check admin role
        user = await self.find_user_by_id(user_id)
        if user is not None and self.is_admin(user):
            return
        if self.is_owner(resource, user_id):
            return
        raise SupersetSecurityException(
            message="You don't have permission to edit this resource. "
            "Only owners and admins can modify it."
        )

    # --- Guest user checks ---

    def is_guest_user(self, user: Any | None = None) -> bool:
        """Check if the given user is a guest user (JWT-authenticated).

        Mirrors the original ``SupersetSecurityManager.is_guest_user``:
        returns False unless the EMBEDDED_SUPERSET feature flag is enabled.
        """
        if not self._embedded_superset_enabled:
            return False
        if user is None:
            return False
        return getattr(user, "is_guest", False)

    async def has_guest_access(self, dashboard: Any, *, user: Any) -> bool:
        """Check if a guest user has access to a specific dashboard."""
        if not self.is_guest_user(user):
            return False
        resources = getattr(user, "resources", [])
        # Check integer ID first (matches Superset priority)
        dashboard_id = getattr(dashboard, "id", None)
        if dashboard_id is not None:
            for r in resources:
                if r.get("type") == "dashboard" and str(r.get("id")) == str(
                    dashboard_id
                ):
                    return True
        # Then check UUID from embedded config
        embedded = getattr(dashboard, "embedded", None)
        if embedded:
            embedded_uuid = str(embedded[0].uuid)
            for r in resources:
                if r.get("type") == "dashboard" and str(r.get("id")) == embedded_uuid:
                    return True
        return False

    # --- Anonymous/Public user ---

    def get_anonymous_user(self) -> Any:
        """Return an AnonymousUser with the PUBLIC role."""
        from superset.middleware.auth import UnauthenticatedUser

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
        datasource = getattr(chart, "datasource", None)
        if datasource:
            return await self.can_access_datasource(datasource, user=user)
        return False

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
        audience: str = "",
    ) -> str:
        """Create a guest access JWT token.

        Delegates to superset.security.guest.create_guest_access_token.
        Controllers call this via security_manager.create_guest_access_token().
        """
        from superset.security.guest import create_guest_access_token

        return create_guest_access_token(
            secret_key=secret_key,
            user=user,
            resources=resources,
            rls=rls,
            exp_seconds=exp_seconds,
            audience=audience,
        )

    @staticmethod
    def parse_jwt_guest_token(
        token: str,
        secret_key: str,
        algorithm: str = "HS256",
    ) -> dict[str, Any] | None:
        """Parse and validate a guest JWT token.

        Delegates to superset.security.guest.parse_guest_token.
        """
        from superset.security.guest import parse_guest_token

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
