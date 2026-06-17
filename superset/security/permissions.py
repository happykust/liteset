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
"""Permission constants and helpers for Superset security.

Mirrors superset/security/permissions.py constants without importing
from superset, ensuring superset can operate independently.
"""

from __future__ import annotations

# --- Access type constants ---
DATABASE_ACCESS: str = "database_access"
SCHEMA_ACCESS: str = "schema_access"
DATASOURCE_ACCESS: str = "datasource_access"
CATALOG_ACCESS: str = "catalog_access"
ALL_DATABASE_ACCESS: str = "all_database_access"
ALL_DATASOURCE_ACCESS: str = "all_datasource_access"
ALL_QUERY_ACCESS: str = "all_query_access"

# --- Action constants ---
CAN_READ: str = "can_read"
CAN_WRITE: str = "can_write"
CAN_DELETE: str = "can_delete"
CAN_EXPLORE: str = "can_explore"
CAN_SQLLAB: str = "can_sqllab"
CAN_CSV: str = "can_csv"
CAN_SHARE_DASHBOARD: str = "can_share_dashboard"
CAN_SHARE_CHART: str = "can_share_chart"
CAN_GRANT_ACCESS: str = "can_grant_access"
CAN_OVERRIDE_ROLE_PERMISSIONS: str = "can_override_role_permissions"
CAN_APPROVE: str = "can_approve"

# --- Resource view menu names ---
CHART_VIEW: str = "Chart"
DASHBOARD_VIEW: str = "Dashboard"
DATABASE_VIEW: str = "Database"
DATASET_VIEW: str = "Dataset"
QUERY_VIEW: str = "Query"
SAVEDQUERY_VIEW: str = "SavedQuery"
ANNOTATION_VIEW: str = "Annotation"
ANNOTATION_LAYER_VIEW: str = "AnnotationLayer"
CSS_TEMPLATE_VIEW: str = "CssTemplate"
REPORT_SCHEDULE_VIEW: str = "ReportSchedule"
LOG_VIEW: str = "Log"
TAG_VIEW: str = "Tag"

# --- Permission sets ---

# Permissions accessible to all authenticated users (Alpha, Gamma, etc.)
ACCESSIBLE_PERMS: frozenset[str] = frozenset(
    {
        "can_userinfo",
        "resetmypassword",
        "can_recent_activity",
    }
)

ADMIN_ONLY_PERMISSIONS: frozenset[str] = frozenset(
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

ADMIN_ONLY_VIEW_MENUS: frozenset[str] = frozenset(
    {
        "Access Requests",
        "Action Logs",
        "Log",
        "List Users",
        "UsersListView",
        "List Roles",
        "List Groups",
        "ResetPasswordView",
        "RoleModelView",
        "UserGroupModelView",
        "Row Level Security",
        "Row Level Security Filters",
        "Security",
        "SQL Lab",
        "User Registrations",
        "User's Statistics",
        # Guarding all AB_ADD_SECURITY_API = True REST APIs
        "RoleRestAPI",
        "Group",
        "Role",
        "Permission",
        "PermissionViewMenu",
        "ViewMenu",
        "User",
        # USER_MODEL_VIEWS
        "RegisterUserModelView",
        "UserDBModelView",
        "UserLDAPModelView",
        "UserInfoEditView",
        "UserOAuthModelView",
        "UserOIDModelView",
        "UserRemoteUserModelView",
    }
)

ALPHA_ONLY_PERMISSIONS: frozenset[str] = frozenset(
    {
        "muldelete",
        "all_database_access",
        "all_datasource_access",
    }
)

ALPHA_ONLY_VIEW_MENUS: frozenset[str] = frozenset(
    {
        "Alerts & Report",
        "Annotation Layers",
        "Annotation",
        "CSS Templates",
        "ColumnarToDatabaseView",
        "CssTemplate",
        "ExcelToDatabaseView",
        "Import dashboards",
        "ImportExportRestApi",
        "Manage",
        "Queries",
        "ReportSchedule",
    }
)

# Specific (permission, view_menu) pairs that are Alpha-only
ALPHA_ONLY_PMVS: frozenset[tuple[str, str]] = frozenset(
    {
        ("can_upload", "Database"),
    }
)

# Permissions that are assigned per-object (datasource, database, schema, etc.)
OBJECT_SPEC_PERMISSIONS: frozenset[str] = frozenset(
    {
        DATABASE_ACCESS,
        SCHEMA_ACCESS,
        DATASOURCE_ACCESS,
        CATALOG_ACCESS,
    }
)

# Data-access permissions (should be preserved on Public role merge)
DATA_ACCESS_PERMISSIONS: frozenset[str] = frozenset(
    {
        DATABASE_ACCESS,
        SCHEMA_ACCESS,
        DATASOURCE_ACCESS,
        CATALOG_ACCESS,
        ALL_DATABASE_ACCESS,
        ALL_DATASOURCE_ACCESS,
        ALL_QUERY_ACCESS,
    }
)

# SQL-Lab-only permissions: (permission_name, view_menu_name) tuples
SQLLAB_ONLY_PERMISSIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("can_read", "SavedQuery"),
        ("can_write", "SavedQuery"),
        ("can_export", "SavedQuery"),
        ("can_read", "Query"),
        ("can_export_csv", "Query"),
        ("can_get_results", "SQLLab"),
        ("can_execute_sql_query", "SQLLab"),
        ("can_estimate_query_cost", "SQL Lab"),
        ("can_export_csv", "SQLLab"),
        ("can_read", "SQLLab"),
        ("can_sqllab_history", "Superset"),
        ("can_sqllab", "Superset"),
        ("can_test_conn", "Superset"),  # Deprecated permission remove on 3.0.0
        ("can_activate", "TabStateView"),
        ("can_get", "TabStateView"),
        ("can_delete_query", "TabStateView"),
        ("can_post", "TabStateView"),
        ("can_delete", "TabStateView"),
        ("can_put", "TabStateView"),
        ("can_migrate_query", "TabStateView"),
        ("menu_access", "SQL Lab"),
        ("menu_access", "SQL Editor"),
        ("menu_access", "Saved Queries"),
        ("menu_access", "Query Search"),
        ("can_read", "SqlLabPermalinkRestApi"),
        ("can_write", "SqlLabPermalinkRestApi"),
        ("can_post", "TableSchemaView"),
        ("can_expanded", "TableSchemaView"),
        ("can_delete", "TableSchemaView"),
    }
)

# SQL-Lab extra permission views (assigned to sql_lab role, not only-SQL-Lab)
SQLLAB_EXTRA_PERMISSION_VIEWS: frozenset[tuple[str, str]] = frozenset(
    {
        ("can_csv", "Superset"),  # Deprecated permission remove on 3.0.0
        ("can_read", "Superset"),
        ("can_read", "Database"),
    }
)

# Read-only permission names
READ_ONLY_PERMISSION: frozenset[str] = frozenset(
    {
        "can_show",
        "can_list",
        "can_get",
        "can_external_metadata",
        "can_external_metadata_by_name",
        "can_read",
        "can_get_drill_info",
    }
)

# Alias for backward compatibility
READ_ONLY_PERMISSIONS: frozenset[str] = READ_ONLY_PERMISSION

# Model views that are read-only (non-read permissions are Admin-only)
READ_ONLY_MODEL_VIEWS: frozenset[str] = frozenset(
    {
        "Database",
        "DynamicPlugin",
    }
)

# Model views that are read-only for Gamma (Alpha gets write access)
GAMMA_READ_ONLY_MODEL_VIEWS: frozenset[str] = frozenset(
    {
        "Dataset",
        "Datasource",
    }
)

# Custom permission views that should always be created in the DB.
# Includes the 12 custom PVMs from ``create_custom_permissions``, the
# ``Superset``-view permissions FAB auto-creates for the explore/guest
# endpoints (``can_explore_json``/``can_explore``), and the FAB-standard
# ``UserInfoView`` perms.
CUSTOM_PERMISSION_VIEWS: frozenset[tuple[str, str]] = frozenset(
    {
        ("can_csv", "Superset"),
        ("can_share_dashboard", "Superset"),
        ("can_share_chart", "Superset"),
        ("can_sqllab", "Superset"),
        ("can_view_query", "Dashboard"),
        ("can_view_chart_as_table", "Dashboard"),
        ("can_drill", "Dashboard"),
        ("can_tag", "Chart"),
        ("can_tag", "Dashboard"),
        # FAB-registered Superset-view + security endpoints
        ("can_explore_json", "Superset"),
        ("can_explore", "Superset"),
        ("can_grant_guest_token", "SecurityRestApi"),
        ("can_userinfo", "UserInfoView"),
        ("resetmypassword", "UserInfoView"),
    }
)
