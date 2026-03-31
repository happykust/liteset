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
    }
)

ADMIN_ONLY_PERMISSIONS: frozenset[str] = frozenset(
    {
        CAN_GRANT_ACCESS,
        CAN_OVERRIDE_ROLE_PERMISSIONS,
        CAN_APPROVE,
        "can_sync_druid_source",
        "can_this_form_post",
        "can_this_form_get",
        "resetpasswords",
        "userinfoedit",
        "all_datasource_access",
        "all_database_access",
        "all_query_access",
        "can_warm_up_cache",
    }
)

ADMIN_ONLY_VIEW_MENUS: frozenset[str] = frozenset(
    {
        "Security",
        "AccessRequestsModelView",
        "Manage",
        "SQL Lab",
        "Queries",
        "RoleModelView",
        "UserDBModelView",
        "ResetMyPasswordView",
        "ResetPasswordView",
        "UserInfoEditView",
        "SecurityRestApi",
    }
)

ALPHA_ONLY_PERMISSIONS: frozenset[str] = frozenset(
    {
        "muldelete",
        "all_datasource_access",
        "all_database_access",
        "all_query_access",
    }
)

ALPHA_ONLY_VIEW_MENUS: frozenset[str] = frozenset(
    {
        "ReportSchedule",
        "Annotation",
        "AnnotationLayer",
        "CssTemplate",
        "ImportExportRestApi",
        "Upload",
    }
)

# Specific (permission, view_menu) pairs that are Alpha-only
ALPHA_ONLY_PMVS: frozenset[tuple[str, str]] = frozenset(
    {
        ("can_write", "Dashboard"),
        ("can_write", "Chart"),
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
        ("can_read", "Query"),
        ("can_write", "Query"),
        ("can_sqllab", "Superset"),
        ("can_sqllab", "SqlLab"),
        ("can_only_my_queries", "SqlLab"),
        ("can_read", "SQLLab"),
        ("can_write", "SQLLab"),
        ("can_execute_sql_query", "SQLLab"),
    }
)

# SQL-Lab extra permission views (assigned to sql_lab role, not only-SQL-Lab)
SQLLAB_EXTRA_PERMISSION_VIEWS: frozenset[tuple[str, str]] = frozenset(
    {
        ("can_csv", "Superset"),
        ("can_read", "CsvToDatabaseView"),
        ("can_read", "ExcelToDatabaseView"),
        ("can_read", "ColumnarToDatabaseView"),
        ("menu_access", "SQL Lab"),
        ("menu_access", "Query Search"),
        ("menu_access", "Saved Queries"),
    }
)

# Read-only permission names
READ_ONLY_PERMISSION: frozenset[str] = frozenset(
    {
        CAN_READ,
        "can_get",
        "can_info",
        "can_list",
        "can_show",
        CAN_CSV,
        CAN_EXPLORE,
        CAN_SHARE_DASHBOARD,
        CAN_SHARE_CHART,
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

# Custom permission views that should always be created in the DB
CUSTOM_PERMISSION_VIEWS: frozenset[tuple[str, str]] = frozenset(
    {
        ("can_share_dashboard", "Superset"),
        ("can_share_chart", "Superset"),
        ("can_csv", "Superset"),
        ("can_explore_json", "Superset"),
        ("can_explore", "Superset"),
        ("can_sqllab", "Superset"),
        ("can_grant_guest_token", "SecurityRestApi"),
        ("can_userinfo", "UserInfoView"),
        ("resetmypassword", "UserInfoView"),
    }
)
