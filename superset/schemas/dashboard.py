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
"""msgspec Structs for the Dashboard API — replaces Marshmallow schemas."""

# ruff: noqa: N815  — camelCase field names required for JSON API contract parity
from __future__ import annotations

from typing import Any

import msgspec

from superset.schemas.base import ApiListResponse, ApiResponse

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class DashboardPostSchema(msgspec.Struct):
    """POST /api/v1/dashboard/"""

    dashboard_title: str | None = None
    slug: str | None = None
    position_json: str | None = None
    css: str | None = None
    json_metadata: str | None = None
    published: bool = False
    certified_by: str | None = None
    certification_details: str | None = None
    is_managed_externally: bool = False
    external_url: str | None = None
    owners: list[int] | None = None
    roles: list[int] | None = None
    tags: list[int] | None = None
    theme_id: int | None = None
    uuid: str | None = None


class DashboardPutSchema(msgspec.Struct):
    """PUT /api/v1/dashboard/<pk>"""

    dashboard_title: str | None | msgspec.UnsetType = msgspec.UNSET
    slug: str | None | msgspec.UnsetType = msgspec.UNSET
    position_json: str | None | msgspec.UnsetType = msgspec.UNSET
    css: str | None | msgspec.UnsetType = msgspec.UNSET
    json_metadata: str | None | msgspec.UnsetType = msgspec.UNSET
    published: bool | None | msgspec.UnsetType = msgspec.UNSET
    certified_by: str | None | msgspec.UnsetType = msgspec.UNSET
    certification_details: str | None | msgspec.UnsetType = msgspec.UNSET
    is_managed_externally: bool | None | msgspec.UnsetType = msgspec.UNSET
    external_url: str | None | msgspec.UnsetType = msgspec.UNSET
    owners: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    roles: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    tags: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    theme_id: int | None | msgspec.UnsetType = msgspec.UNSET
    uuid: str | None | msgspec.UnsetType = msgspec.UNSET


class DashboardCopySchema(msgspec.Struct):
    """POST /api/v1/dashboard/<pk>/copy/"""

    dashboard_title: str
    json_metadata: str
    css: str | None = None
    duplicate_slices: bool = False


class DashboardFiltersUpdateSchema(msgspec.Struct):
    """PUT /api/v1/dashboard/<pk>/filters"""

    deleted: list[str] = []
    modified: list[dict[str, Any]] = []
    reordered: list[str] = []


class DashboardColorsUpdateSchema(msgspec.Struct):
    """PUT /api/v1/dashboard/<pk>/colors"""

    color_namespace: str | None = None
    color_scheme: str | None = None
    map_label_colors: dict[str, str] = {}
    shared_label_colors: dict[str, str] = {}
    label_colors: dict[str, str] = {}
    color_scheme_domain: list[str] = []


class DashboardScreenshotSchema(msgspec.Struct):
    """POST /api/v1/dashboard/<pk>/cache_dashboard_screenshot/"""

    dataMask: dict[str, Any] = {}
    activeTabs: list[str] = []
    anchor: str | None = None
    urlParams: list[list[str]] = []


class DashboardPermalinkSchema(msgspec.Struct):
    """POST /api/v1/dashboard/<pk>/permalink"""

    dataMask: dict[str, Any] = {}
    activeTabs: list[str] = []
    anchor: str | None = None
    urlParams: list[list[str]] = []


class FilterStateSchema(msgspec.Struct):
    """POST/PUT filter state value."""

    value: str
    tab_id: int | None = None


# ---------------------------------------------------------------------------
# Metadata schemas
# ---------------------------------------------------------------------------


class DashboardJSONMetadata(msgspec.Struct):
    """Parsed JSON metadata for a dashboard."""

    filter_scopes: dict[str, Any] = {}
    default_filters: str = "{}"
    timed_refresh_immune_slices: list[int] = []
    expanded_slices: dict[str, bool] = {}
    refresh_frequency: int = 0
    color_scheme: str | None = None
    label_colors: dict[str, str] = {}
    shared_label_colors: dict[str, str] = {}
    map_label_colors: dict[str, str] = {}
    color_namespace: str | None = None
    color_scheme_domain: list[str] = []
    cross_filters_enabled: bool = True
    native_filter_configuration: list[dict[str, Any]] = []
    chart_configuration: dict[str, Any] = {}
    global_chart_configuration: dict[str, Any] = {}
    stagger_refresh: bool = False
    stagger_time: int = 0
    filter_bar_orientation: str | None = None
    positions: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Embedded dashboard
# ---------------------------------------------------------------------------


class EmbeddedDashboardConfig(msgspec.Struct):
    """Configuration for an embedded dashboard."""

    allowed_domains: list[str] = []


class EmbeddedDashboardResponse(msgspec.Struct):
    """Response for embedded dashboard endpoints."""

    uuid: str
    allowed_domains: list[str] = []
    dashboard_id: str | None = None
    changed_on: str | None = None
    changed_by: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


DashboardGetResponse = ApiResponse
DashboardListResponse = ApiListResponse


# ---------------------------------------------------------------------------
# Utility schemas
# ---------------------------------------------------------------------------


class TabInfo(msgspec.Struct):
    """Tab information within a dashboard."""

    tab_id: str
    tab_title: str
    charts: list[int] = []


class DashboardDataset(msgspec.Struct):
    """Dataset reference within a dashboard."""

    id: int
    uid: str | None = None
    column_names: list[str] = []
    verbose_map: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------


class ImportV1Dashboard(msgspec.Struct):
    """Import payload for a dashboard."""

    dashboard_title: str
    uuid: str
    description: str | None = None
    css: str | None = None
    slug: str | None = None
    position: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    version: str = "1.0.0"
    is_managed_externally: bool = False
    external_url: str | None = None
    certified_by: str | None = None
    certification_details: str | None = None
    published: bool | None = None
    tags: list[str] | None = None
    theme_uuid: str | None = None
