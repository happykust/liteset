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
"""Permission constants and helpers for Liteset security.

Mirrors superset/security/permissions.py constants without importing
from superset, ensuring liteset can operate independently.
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
ADMIN_ONLY_PERMISSIONS: frozenset[str] = frozenset(
    {
        CAN_GRANT_ACCESS,
        CAN_OVERRIDE_ROLE_PERMISSIONS,
        CAN_APPROVE,
        "can_sync_druid_source",
        "menu_access",
        "can_this_form_post",
        "can_this_form_get",
        "resetmypassword",
        "resetpasswords",
        "userinfoedit",
        "all_datasource_access",
        "all_database_access",
        "all_query_access",
    }
)

READ_ONLY_PERMISSIONS: frozenset[str] = frozenset(
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
