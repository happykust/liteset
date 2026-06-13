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
"""Per-resource descriptors for the ``GET /<resource>/_info`` builder.

Each ``ResourceSpec`` describes the *static contract* of one resource:
the Marshmallow-style ``add_columns`` / ``edit_columns`` payloads (with
their validators, types, required/unique flags) and the canonical list
of ``search_columns`` plus any custom search filters that the original
Apache Superset declared via ``search_filters = {col: [Filter, ...]}``.

The dynamic parts — filter operator catalogues per column (deduced via
SQLAlchemy introspection in :mod:`superset.info_builder.operators`),
``permissions`` (resolved per request from RBAC), and ``add_title`` /
``edit_title`` (auto-generated from the model's class name) — are added
by :mod:`superset.info_builder.builder`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytz

from superset.info_builder.marshmallow_emit import (
    function,
    length,
    one_of,
    range_,
)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One entry in ``add_columns`` / ``edit_columns``."""

    name: str
    type: str
    required: bool = False
    unique: bool = False
    description: str = ""
    validate: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """Static descriptor of one ``_info`` resource."""

    add_columns: list[FieldSpec]
    add_title: str
    edit_columns: list[FieldSpec]
    edit_title: str
    search_columns: list[str]
    search_filters_custom: dict[str, list[dict[str, str]]]


# Resources are keyed by the canonical model class name, matching the
# ``model_name`` argument passed to ``get_info_payload`` from each
# controller.
RESOURCE_SPECS: dict[str, ResourceSpec] = {
    # ==================================================
    # AnnotationLayer
    # ==================================================
    "AnnotationLayer": ResourceSpec(
        add_columns=[
            FieldSpec(
                name="short_descr",
                type="String",
                required=True,
                validate=[length(min_=1, max_=500)],
            ),
            FieldSpec(name="long_descr", type="String"),
            FieldSpec(name="start_dttm", type="DateTime", required=True),
            FieldSpec(name="end_dttm", type="DateTime", required=True),
            FieldSpec(
                name="json_metadata",
                type="String",
                validate=[function("validate_json")],
            ),
        ],
        add_title="Add Annotation",
        edit_columns=[
            FieldSpec(
                name="short_descr", type="String", validate=[length(min_=1, max_=500)]
            ),
            FieldSpec(name="long_descr", type="String"),
            FieldSpec(name="start_dttm", type="DateTime"),
            FieldSpec(name="end_dttm", type="DateTime"),
            FieldSpec(
                name="json_metadata",
                type="String",
                validate=[function("validate_json")],
            ),
        ],
        edit_title="Edit Annotation",
        search_columns=[
            "changed_by",
            "created_by",
            "json_metadata",
            "layer",
            "long_descr",
            "short_descr",
        ],
        search_filters_custom={
            "short_descr": [
                {"name": "All Text", "operator": "annotation_all_text"},
            ],
        },
    ),
    # ==================================================
    # Chart
    # ==================================================
    "Chart": ResourceSpec(
        add_columns=[
            FieldSpec(
                name="slice_name",
                type="String",
                required=True,
                validate=[length(min_=1, max_=250)],
            ),
        ],
        add_title="Add Slice",
        edit_columns=[
            FieldSpec(
                name="slice_name", type="String", validate=[length(min_=0, max_=250)]
            ),
        ],
        edit_title="Edit Slice",
        search_columns=[
            "changed_by",
            "created_by",
            "dashboards",
            "datasource_id",
            "datasource_name",
            "datasource_type",
            "description",
            "id",
            "last_saved_at",
            "last_saved_by",
            "owners",
            "slice_name",
            "tags",
            "uuid",
            "viz_type",
        ],
        search_filters_custom={
            "created_by": [
                {"name": "Has created by", "operator": "chart_has_created_by"},
                {"name": "Created by me", "operator": "chart_created_by_me"},
            ],
            # The favorite / certified / owned filters key off ``id`` in
            # upstream ChartRestApi.search_filters — surface them in _info so
            # clients can discover the operators.
            "id": [
                {"name": "Is favorite", "operator": "chart_is_favorite"},
                {"name": "Is certified", "operator": "chart_is_certified"},
                {
                    "name": "Owned Created or Favored",
                    "operator": "chart_owned_created_favored_by_me",
                },
            ],
            "slice_name": [
                {"name": "All Text", "operator": "chart_all_text"},
            ],
            "tags": [
                {"name": "Is tagged", "operator": "chart_tags"},
                {"name": "Is tagged", "operator": "chart_tag_id"},
            ],
        },
    ),
    # ==================================================
    # CssTemplate
    # ==================================================
    "CssTemplate": ResourceSpec(
        add_columns=[
            FieldSpec(name="css", type="String"),
            FieldSpec(
                name="template_name",
                type="String",
                validate=[length(min_=None, max_=250)],
            ),
        ],
        add_title="Add Css Template",
        edit_columns=[
            FieldSpec(name="css", type="String"),
            FieldSpec(
                name="template_name",
                type="String",
                validate=[length(min_=None, max_=250)],
            ),
        ],
        edit_title="Edit Css Template",
        search_columns=[
            "changed_by",
            "created_by",
            "css",
            "template_name",
        ],
        search_filters_custom={
            "template_name": [
                {"name": "All Text", "operator": "css_template_all_text"},
            ],
        },
    ),
    # ==================================================
    # Dashboard
    # ==================================================
    "Dashboard": ResourceSpec(
        add_columns=[
            FieldSpec(name="certified_by", type="String"),
            FieldSpec(name="certification_details", type="String"),
            FieldSpec(
                name="dashboard_title",
                type="String",
                validate=[length(min_=0, max_=500)],
            ),
            FieldSpec(name="slug", type="String", validate=[length(min_=1, max_=255)]),
            FieldSpec(name="owners", type="List"),
            FieldSpec(name="roles", type="List"),
            FieldSpec(
                name="position_json",
                type="String",
                validate=[function("validate_json")],
            ),
            FieldSpec(name="css", type="String"),
            FieldSpec(name="theme_id", type="Integer"),
            FieldSpec(
                name="json_metadata",
                type="String",
                validate=[function("validate_json_metadata")],
            ),
            FieldSpec(name="published", type="Boolean"),
        ],
        add_title="Add Dashboard",
        edit_columns=[
            FieldSpec(name="certified_by", type="String"),
            FieldSpec(name="certification_details", type="String"),
            FieldSpec(
                name="dashboard_title",
                type="String",
                validate=[length(min_=0, max_=500)],
            ),
            FieldSpec(name="slug", type="String", validate=[length(min_=0, max_=255)]),
            FieldSpec(name="owners", type="List"),
            FieldSpec(name="roles", type="List"),
            FieldSpec(
                name="position_json",
                type="String",
                validate=[function("validate_json")],
            ),
            FieldSpec(name="css", type="String"),
            FieldSpec(name="theme_id", type="Integer"),
            FieldSpec(
                name="json_metadata",
                type="String",
                validate=[function("validate_json_metadata")],
            ),
            FieldSpec(name="published", type="Boolean"),
        ],
        edit_title="Edit Dashboard",
        search_columns=[
            "changed_by",
            "created_by",
            "dashboard_title",
            "id",
            "owners",
            "published",
            "roles",
            "slug",
            "tags",
            "uuid",
        ],
        search_filters_custom={
            "created_by": [
                {"name": "Created by me", "operator": "dashboard_created_by_me"},
                {"name": "Has created by", "operator": "dashboard_has_created_by"},
            ],
            "dashboard_title": [
                {"name": "Title or Slug", "operator": "title_or_slug"},
            ],
            # Favorite / certified filters key off ``id`` upstream.
            "id": [
                {"name": "Is favorite", "operator": "dashboard_is_favorite"},
                {"name": "Is certified", "operator": "dashboard_is_certified"},
            ],
            "tags": [
                {"name": "Is tagged", "operator": "dashboard_tag_id"},
                {"name": "Is tagged", "operator": "dashboard_tags"},
            ],
        },
    ),
    # ==================================================
    # SqlaTable
    # ==================================================
    "SqlaTable": ResourceSpec(
        add_columns=[
            FieldSpec(name="database", type="Integer", required=True),
            FieldSpec(
                name="catalog", type="String", validate=[length(min_=0, max_=250)]
            ),
            FieldSpec(
                name="schema", type="String", validate=[length(min_=0, max_=250)]
            ),
            FieldSpec(
                name="table_name",
                type="String",
                required=True,
                validate=[length(min_=1, max_=250)],
            ),
            FieldSpec(name="sql", type="String"),
            FieldSpec(name="owners", type="List"),
        ],
        add_title="Add Sqla Table",
        edit_columns=[
            FieldSpec(
                name="table_name", type="String", validate=[length(min_=1, max_=250)]
            ),
            FieldSpec(name="sql", type="String"),
            FieldSpec(name="filter_select_enabled", type="Boolean"),
            FieldSpec(
                name="fetch_values_predicate",
                type="String",
                validate=[length(min_=0, max_=1000)],
            ),
            FieldSpec(
                name="catalog", type="String", validate=[length(min_=0, max_=250)]
            ),
            FieldSpec(
                name="schema", type="String", validate=[length(min_=0, max_=255)]
            ),
            FieldSpec(name="description", type="String"),
            FieldSpec(name="main_dttm_col", type="String"),
            FieldSpec(name="normalize_columns", type="Boolean"),
            FieldSpec(name="always_filter_main_dttm", type="Boolean"),
            FieldSpec(name="offset", type="Integer"),
            FieldSpec(name="default_endpoint", type="String"),
            FieldSpec(name="cache_timeout", type="Integer"),
            FieldSpec(name="is_sqllab_view", type="Boolean"),
            FieldSpec(name="template_params", type="String"),
            FieldSpec(name="owners", type="List"),
            FieldSpec(name="columns", type="List"),
            FieldSpec(name="metrics", type="List"),
            FieldSpec(name="extra", type="String"),
        ],
        edit_title="Edit Sqla Table",
        search_columns=[
            "catalog",
            "changed_by",
            "created_by",
            "database",
            "id",
            "owners",
            "schema",
            "sql",
            "table_name",
            "uuid",
        ],
        search_filters_custom={
            # Certified filter keys off ``id`` upstream (DatasetRestApi).
            "id": [
                {"name": "Is certified", "operator": "dataset_is_certified"},
            ],
            "sql": [
                {"name": "Null or Empty", "operator": "dataset_is_null_or_empty"},
            ],
        },
    ),
    # ==================================================
    # ReportSchedule
    # ==================================================
    "ReportSchedule": ResourceSpec(
        add_columns=[
            FieldSpec(name="active", type="Boolean"),
            FieldSpec(name="chart", type="Integer"),
            FieldSpec(name="context_markdown", type="String"),
            FieldSpec(name="creation_method", type="Enum"),
            FieldSpec(
                name="crontab",
                type="String",
                required=True,
                validate=[function("validate_crontab"), length(min_=1, max_=1000)],
            ),
            FieldSpec(name="custom_width", type="Integer"),
            FieldSpec(name="dashboard", type="Integer"),
            FieldSpec(name="database", type="Integer"),
            FieldSpec(name="description", type="String"),
            FieldSpec(name="extra", type="Dict"),
            FieldSpec(name="force_screenshot", type="Boolean"),
            FieldSpec(
                name="grace_period",
                type="Integer",
                validate=[
                    range_(
                        min_=1,
                        max_=None,
                        min_inclusive=True,
                        max_inclusive=True,
                        error="Value must be greater than 0",
                    )
                ],
            ),
            FieldSpec(
                name="log_retention",
                type="Integer",
                validate=[
                    range_(
                        min_=1,
                        max_=None,
                        min_inclusive=True,
                        max_inclusive=True,
                        error="Value must be greater than 0",
                    )
                ],
            ),
            FieldSpec(
                name="name",
                type="String",
                required=True,
                validate=[length(min_=1, max_=150)],
            ),
            FieldSpec(name="owners", type="List"),
            FieldSpec(name="recipients", type="List"),
            FieldSpec(
                name="report_format",
                type="String",
                validate=[one_of(choices=("PDF", "PNG", "CSV", "TEXT"))],
            ),
            FieldSpec(name="sql", type="String"),
            FieldSpec(
                name="timezone", type="String", validate=[one_of(pytz.all_timezones)]
            ),
            FieldSpec(
                name="type",
                type="String",
                required=True,
                validate=[one_of(choices=("Alert", "Report"))],
            ),
            FieldSpec(name="validator_config_json", type="Nested"),
            FieldSpec(
                name="validator_type",
                type="String",
                validate=[one_of(choices=("not null", "operator"))],
            ),
            FieldSpec(
                name="working_timeout",
                type="Integer",
                validate=[
                    range_(
                        min_=1,
                        max_=None,
                        min_inclusive=True,
                        max_inclusive=True,
                        error="Value must be greater than 0",
                    )
                ],
            ),
        ],
        add_title="Add Report Schedule",
        edit_columns=[
            FieldSpec(name="active", type="Boolean"),
            FieldSpec(name="chart", type="Integer"),
            FieldSpec(name="context_markdown", type="String"),
            FieldSpec(name="creation_method", type="Enum"),
            FieldSpec(
                name="crontab",
                type="String",
                validate=[function("validate_crontab"), length(min_=1, max_=1000)],
            ),
            FieldSpec(name="custom_width", type="Integer"),
            FieldSpec(name="dashboard", type="Integer"),
            FieldSpec(name="database", type="Integer"),
            FieldSpec(name="description", type="String"),
            FieldSpec(name="extra", type="Dict"),
            FieldSpec(name="force_screenshot", type="Boolean"),
            FieldSpec(
                name="grace_period",
                type="Integer",
                validate=[
                    range_(
                        min_=1,
                        max_=None,
                        min_inclusive=True,
                        max_inclusive=True,
                        error="Value must be greater than 0",
                    )
                ],
            ),
            FieldSpec(
                name="log_retention",
                type="Integer",
                validate=[
                    range_(
                        min_=0,
                        max_=None,
                        min_inclusive=True,
                        max_inclusive=True,
                        error="Value must be 0 or greater",
                    )
                ],
            ),
            FieldSpec(name="name", type="String", validate=[length(min_=1, max_=150)]),
            FieldSpec(name="owners", type="List"),
            FieldSpec(name="recipients", type="List"),
            FieldSpec(
                name="report_format",
                type="String",
                validate=[one_of(choices=("PDF", "PNG", "CSV", "TEXT"))],
            ),
            FieldSpec(name="sql", type="String"),
            FieldSpec(
                name="timezone", type="String", validate=[one_of(pytz.all_timezones)]
            ),
            FieldSpec(
                name="type",
                type="String",
                validate=[one_of(choices=("Alert", "Report"))],
            ),
            FieldSpec(name="validator_config_json", type="Nested"),
            FieldSpec(
                name="validator_type",
                type="String",
                validate=[one_of(choices=("not null", "operator"))],
            ),
            FieldSpec(
                name="working_timeout",
                type="Integer",
                validate=[
                    range_(
                        min_=1,
                        max_=None,
                        min_inclusive=True,
                        max_inclusive=True,
                        error="Value must be greater than 0",
                    )
                ],
            ),
        ],
        edit_title="Edit Report Schedule",
        search_columns=[
            "active",
            "changed_by",
            "chart_id",
            "created_by",
            "creation_method",
            "dashboard_id",
            "last_state",
            "name",
            "owners",
            "type",
        ],
        search_filters_custom={
            "name": [
                {"name": "All Text", "operator": "report_all_text"},
            ],
        },
    ),
    # ==================================================
    # SavedQuery
    # ==================================================
    "SavedQuery": ResourceSpec(
        add_columns=[
            FieldSpec(name="db_id", type="Inferred"),
            FieldSpec(name="description", type="String"),
            FieldSpec(
                name="label", type="String", validate=[length(min_=None, max_=256)]
            ),
            FieldSpec(
                name="catalog", type="String", validate=[length(min_=None, max_=256)]
            ),
            FieldSpec(
                name="schema", type="String", validate=[length(min_=None, max_=128)]
            ),
            FieldSpec(name="sql", type="String"),
            FieldSpec(name="template_parameters", type="String"),
            FieldSpec(name="extra_json", type="String"),
        ],
        add_title="Add Saved Query",
        edit_columns=[
            FieldSpec(name="db_id", type="Inferred"),
            FieldSpec(name="description", type="String"),
            FieldSpec(
                name="label", type="String", validate=[length(min_=None, max_=256)]
            ),
            FieldSpec(
                name="catalog", type="String", validate=[length(min_=None, max_=256)]
            ),
            FieldSpec(
                name="schema", type="String", validate=[length(min_=None, max_=128)]
            ),
            FieldSpec(name="sql", type="String"),
            FieldSpec(name="template_parameters", type="String"),
            FieldSpec(name="extra_json", type="String"),
        ],
        edit_title="Edit Saved Query",
        search_columns=[
            "catalog",
            "changed_by",
            "created_by",
            "database",
            "id",
            "label",
            "schema",
            "tags",
        ],
        search_filters_custom={
            # Favorite filter keys off ``id`` upstream (SavedQueryRestApi).
            "id": [
                {"name": "Is favorite", "operator": "saved_query_is_fav"},
            ],
            "label": [
                {"name": "All Text", "operator": "all_text"},
            ],
            "tags": [
                {"name": "Is tagged", "operator": "saved_query_tags"},
                {"name": "Is tagged", "operator": "saved_query_tag_id"},
            ],
        },
    ),
    # ==================================================
    # Theme
    # ==================================================
    "Theme": ResourceSpec(
        add_columns=[
            FieldSpec(name="json_data", type="String", required=True),
            FieldSpec(name="theme_name", type="String", required=True),
        ],
        add_title="Add Theme",
        edit_columns=[
            FieldSpec(name="json_data", type="String", required=True),
            FieldSpec(name="theme_name", type="String", required=True),
        ],
        edit_title="Edit Theme",
        search_columns=[
            "changed_by",
            "created_by",
            "is_system",
            "is_system_dark",
            "is_system_default",
            "json_data",
            "theme_name",
        ],
        search_filters_custom={
            "theme_name": [
                {"name": "All Text", "operator": "theme_all_text"},
            ],
        },
    ),
}

# Aliases — same spec under multiple keys for caller convenience.
RESOURCE_SPECS["Slice"] = RESOURCE_SPECS["Chart"]
RESOURCE_SPECS["Annotation"] = RESOURCE_SPECS["AnnotationLayer"]
RESOURCE_SPECS["Dataset"] = RESOURCE_SPECS["SqlaTable"]
RESOURCE_SPECS["Report"] = RESOURCE_SPECS["ReportSchedule"]
