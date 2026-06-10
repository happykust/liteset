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

from __future__ import annotations

import re
from typing import Any

import msgspec

from superset.schemas.base import ApiListResponse, ApiResponse, ModelStruct

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


def _sanitize_slug(slug: str | None) -> str | None:
    """Strip whitespace, replace spaces with hyphens, remove non-word chars."""
    if slug is None:
        return None
    slug = slug.strip()
    slug = slug.replace(" ", "-")
    slug = re.sub(r"[^\w\-]+", "", slug)
    return slug or None


def _validate_json_string(
    field_name: str,
    value: Any,
    *,
    allow_empty: bool = True,
) -> None:
    """Raise ``msgspec.ValidationError`` if value is not parseable JSON.

    Mirrors upstream's ``validate_json`` /  ``validate_json_metadata`` at
    superset_old/dashboards/schemas.py:99-115. Empty string is rejected
    when ``allow_empty=False`` (matches marshmallow's ``validate_json``
    on required fields like temporary-cache ``value``); for nullable
    update fields (``json_metadata``, ``position_json``) we accept empty
    as "no change" — the controller layer translates it to None.
    """
    if value is None or value is msgspec.UNSET:
        return
    if not isinstance(value, (str, bytes, bytearray)):
        return
    if value == "" or value == b"":
        if allow_empty:
            return
        raise msgspec.ValidationError(f"{field_name} cannot be empty")
    import json as _json

    try:
        _json.loads(value)
    except (ValueError, TypeError) as ex:
        raise msgspec.ValidationError(f"{field_name} is not valid JSON: {ex}") from ex


def _validate_json_object(field_name: str, value: Any) -> None:
    """Reject a ``json_metadata`` value that isn't a JSON *object*.

    Mirrors upstream's ``validate_json_metadata``
    (superset_old/dashboards/schemas.py:106) which runs the parsed value
    through ``DashboardJSONMetadataSchema().validate(...)`` — a non-mapping
    (``[]`` / ``"str"`` / ``5``) yields ``{"_schema": ["Invalid input
    type."]}`` → 422. Without this, a valid-but-non-object json_metadata
    sails past ``_validate_json_string`` and later crashes
    ``DashboardDAO.set_dash_metadata`` (``md.pop`` / ``md.setdefault`` on a
    list/str) → HTTP 500.
    """
    if value is None or value is msgspec.UNSET:
        return
    if not isinstance(value, (str, bytes, bytearray)) or value in ("", b""):
        return
    import json as _json

    try:
        parsed = _json.loads(value)
    except (ValueError, TypeError):
        return  # parse errors are reported by _validate_json_string
    if not isinstance(parsed, dict):
        raise msgspec.ValidationError(f"{field_name} must be a JSON object")


def _normalize_id_list(
    value: list[int | dict[str, Any]] | None | msgspec.UnsetType,
) -> list[int] | None | msgspec.UnsetType:
    """Accept either ``[1, 2]`` or ``[{"id": 1}, ...]`` and return ``[1, 2]``.

    The frontend sometimes round-trips nested user/role objects from a GET
    response straight back into a PUT body (``owners: r.owners``).  The
    original Superset Marshmallow schema strictly expected ``list[int]`` and
    would 400 on such payloads, but the Cypress suite — disabled in upstream
    CI — relies on this shape working.  We accept both and normalise to
    ids so downstream command code (``compute_owner_list``) receives the
    canonical ``list[int]``.
    """
    if value is msgspec.UNSET or value is None:
        return value
    result: list[int] = []
    for item in value:
        if isinstance(item, int):
            result.append(item)
        elif isinstance(item, dict) and "id" in item:
            result.append(int(item["id"]))
    return result


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
    owners: list[int | dict[str, Any]] | None = None
    roles: list[int | dict[str, Any]] | None = None
    tags: list[int | dict[str, Any]] | None = None
    theme_id: int | None = None
    uuid: str | None = None

    def __post_init__(self) -> None:
        self.slug = _sanitize_slug(self.slug)
        self.owners = _normalize_id_list(self.owners)  # type: ignore[assignment]
        self.roles = _normalize_id_list(self.roles)  # type: ignore[assignment]
        self.tags = _normalize_id_list(self.tags)  # type: ignore[assignment]
        _validate_json_string("position_json", self.position_json)
        _validate_json_string("json_metadata", self.json_metadata)
        _validate_json_object("json_metadata", self.json_metadata)


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
    owners: list[int | dict[str, Any]] | None | msgspec.UnsetType = msgspec.UNSET
    roles: list[int | dict[str, Any]] | None | msgspec.UnsetType = msgspec.UNSET
    tags: list[int | dict[str, Any]] | None | msgspec.UnsetType = msgspec.UNSET
    theme_id: int | None | msgspec.UnsetType = msgspec.UNSET
    uuid: str | None | msgspec.UnsetType = msgspec.UNSET

    def __post_init__(self) -> None:
        if isinstance(self.slug, str):
            self.slug = _sanitize_slug(self.slug)
        self.owners = _normalize_id_list(self.owners)  # type: ignore[assignment]
        self.roles = _normalize_id_list(self.roles)  # type: ignore[assignment]
        self.tags = _normalize_id_list(self.tags)  # type: ignore[assignment]
        _validate_json_string("position_json", self.position_json)
        _validate_json_string("json_metadata", self.json_metadata)
        _validate_json_object("json_metadata", self.json_metadata)


class DashboardCopySchema(msgspec.Struct):
    """POST /api/v1/dashboard/<pk>/copy/"""

    dashboard_title: str
    json_metadata: str
    css: str | None = None
    duplicate_slices: bool = False

    def __post_init__(self) -> None:
        # ``copy_dashboard`` does ``metadata = loads(json_metadata)`` then feeds
        # it to ``set_dash_metadata`` as the ``data`` arg (``data.get(...)``) —
        # a non-object value (``[1,2]`` / ``"s"``) → AttributeError → HTTP 500.
        _validate_json_string("json_metadata", self.json_metadata)
        _validate_json_object("json_metadata", self.json_metadata)


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
    shared_label_colors: list[str] | dict[str, str] = []
    label_colors: dict[str, str] = {}
    color_scheme_domain: list[str] = []

    def __post_init__(self) -> None:
        # 1:1 with ``SharedLabelsColorsField._deserialize``
        # (superset_old/dashboards/schemas.py:130-135): when the value is a
        # dict (backward-compat format), coerce to an empty list so downstream
        # consumers always see a list.  When it's a list with non-string items
        # raise a ValidationError.
        if isinstance(self.shared_label_colors, dict):
            self.shared_label_colors = []
        elif isinstance(self.shared_label_colors, list):
            if not all(isinstance(item, str) for item in self.shared_label_colors):
                raise msgspec.ValidationError("shared_label_colors: Not a valid list")


class DashboardScreenshotSchema(msgspec.Struct, rename="camel"):
    """POST /api/v1/dashboard/<pk>/cache_dashboard_screenshot/"""

    data_mask: dict[str, Any] = {}
    active_tabs: list[str] = []
    anchor: str | None = None
    url_params: list[list[str]] = []
    permalink_key: str | None = None


class DashboardPermalinkSchema(msgspec.Struct, rename="camel"):
    """POST /api/v1/dashboard/<pk>/permalink"""

    data_mask: dict[str, Any] = {}
    active_tabs: list[str] = []
    anchor: str | None = None
    url_params: list[list[str]] = []


class FilterStateSchema(msgspec.Struct):
    """POST/PUT filter state value.

    1:1 with upstream ``TemporaryCachePostSchema`` /
    ``TemporaryCachePutSchema``
    (superset_old/temporary_cache/schemas.py:22-37): ``value`` is
    required, non-empty, and must be parseable JSON (``validate=
    validate_json``). The temporary-cache store doesn't enforce JSON at
    the DB layer, but the frontend loads the value with ``JSON.parse``
    on retrieval — a malformed payload poisons the dashboard URL.

    Note: ``tab_id`` is NOT part of the body schema — the original reads
    it from the query string (``request.args.get("tab_id")``). See the
    controller for the Litestar ``Parameter(query="tab_id")`` binding.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_json_string("value", self.value, allow_empty=False)


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
    shared_label_colors: list[str] | dict[str, str] = []
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
    import_time: int | None = None
    remote_id: int | None = None
    native_filter_migration: dict[str, Any] = {}

    def __post_init__(self) -> None:
        # 1:1 with ``SharedLabelsColorsField._deserialize``
        # (superset_old/dashboards/schemas.py:130-135): when the value is a
        # dict (backward-compat format), coerce to an empty list so downstream
        # consumers always see a list.  When it's a list with non-string items
        # raise a ValidationError.
        if isinstance(self.shared_label_colors, dict):
            self.shared_label_colors = []


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
# Response ref structs (local; consolidate to schemas/base.py later)
# ---------------------------------------------------------------------------


class UserRef(ModelStruct):
    """Lightweight user reference for nested serialisation."""

    id: int
    first_name: str = ""
    last_name: str = ""


class RoleRef(ModelStruct):
    """Lightweight role reference."""

    id: int
    name: str = ""


class TagRef(ModelStruct):
    """Lightweight tag reference."""

    id: int
    name: str = ""
    type: str | None = None


class ThemeRef(ModelStruct):
    """Lightweight theme reference."""

    id: int
    theme_name: str | None = None
    json_data: str | None = None


# ---------------------------------------------------------------------------
# Response result structs
# ---------------------------------------------------------------------------


class DashboardDetailResult(ModelStruct):
    """Full dashboard detail — used by GET /{id}, POST /, PUT /{id}.

    Uses :class:`ModelStruct` auto-mapping for most fields; only non-trivial
    derivations need ``_resolve_*`` overrides.
    """

    id: int
    dashboard_title: str | None = None
    slug: str | None = None
    position_json: str | None = None
    css: str | None = None
    json_metadata: str | None = None
    published: bool = False
    description: str | None = None
    uuid: str | None = None
    url: str | None = None
    status: str | None = None
    thumbnail_url: str | None = None
    certified_by: str | None = None
    certification_details: str | None = None
    is_managed_externally: bool = False
    changed_on: str | None = None
    created_on: str | None = None
    changed_on_delta_humanized: str | None = None
    created_on_delta_humanized: str | None = None
    changed_by_name: str | None = None
    changed_by: UserRef | None = None
    created_by: UserRef | None = None
    owners: list[UserRef] = []
    roles: list[RoleRef] = []
    tags: list[TagRef] = []
    charts: list[str] = []
    table_names: str | None = None
    theme: ThemeRef | None = None

    # -- custom resolvers for non-trivial fields --

    @classmethod
    def _resolve_changed_by_name(cls, obj: Any) -> str | None:
        """Resolve without triggering lazy load on changed_by relationship."""
        from sqlalchemy.orm import attributes

        state = attributes.instance_state(obj)
        # Only read the property if changed_by is already loaded
        if "changed_by" in state.dict:
            cb = obj.changed_by
            return str(cb) if cb else ""
        return None

    @classmethod
    def _resolve_charts(cls, obj: Any) -> list[str]:
        """Charts come from ``dashboard.slices``, mapped to slice_name."""
        slices = getattr(obj, "slices", None) or []
        return [getattr(c, "slice_name", str(c)) for c in slices]

    @classmethod
    def from_model_brief(cls, dashboard: Any) -> DashboardDetailResult:
        """Build a minimal result for POST (create) responses."""
        return cls(
            id=dashboard.id,
            dashboard_title=dashboard.dashboard_title,
            slug=dashboard.slug,
        )


# ---------------------------------------------------------------------------
# Response wrappers
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
    theme_id: int | None = None
