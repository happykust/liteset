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
"""Initial squashed schema -- all 46 Superset tables.

Replaces 344 legacy Alembic migration files with a single SA 2.0
compatible migration that creates the complete database schema.

Revision ID: c233f5365c9e
Revises: (none)
Create Date: 2026-03-28 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c233f5365c9e"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =====================================================================
    # Layer 0: Independent tables (no foreign keys to other app tables)
    # =====================================================================

    # -- ab_permission -------------------------------------------------------
    op.create_table(
        "ab_permission",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=512), unique=True, nullable=False),
    )

    # -- ab_view_menu --------------------------------------------------------
    op.create_table(
        "ab_view_menu",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=512), unique=True, nullable=False),
    )

    # -- ab_role -------------------------------------------------------------
    op.create_table(
        "ab_role",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=256), unique=True, nullable=False),
    )

    # -- ab_user -------------------------------------------------------------
    op.create_table(
        "ab_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("first_name", sa.String(length=256), nullable=False),
        sa.Column("last_name", sa.String(length=256), nullable=False),
        sa.Column("username", sa.String(length=512), unique=True, nullable=False),
        sa.Column("password", sa.String(length=256), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column("email", sa.String(length=512), unique=True, nullable=False),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.Column("login_count", sa.Integer(), nullable=True),
        sa.Column("fail_login_count", sa.Integer(), nullable=True),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
    )

    # -- ab_register_user (FAB table, not in models) -------------------------
    op.create_table(
        "ab_register_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("first_name", sa.String(length=256), nullable=False),
        sa.Column("last_name", sa.String(length=256), nullable=False),
        sa.Column("username", sa.String(length=512), unique=True, nullable=False),
        sa.Column("password", sa.String(length=256), nullable=True),
        sa.Column("email", sa.String(length=512), nullable=False),
        sa.Column("registration_date", sa.DateTime(), nullable=True),
        sa.Column("registration_hash", sa.String(length=256), nullable=True),
    )

    # -- ab_permission_view --------------------------------------------------
    # UniqueConstraint + indexes mirror the FAB model
    # (flask_appbuilder/security/sqla/models.py) so a fresh DB matches FAB's
    # ``create_all`` schema 1:1.
    op.create_table(
        "ab_permission_view",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "permission_id",
            sa.Integer(),
            sa.ForeignKey("ab_permission.id"),
            nullable=True,
        ),
        sa.Column(
            "view_menu_id",
            sa.Integer(),
            sa.ForeignKey("ab_view_menu.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("permission_id", "view_menu_id"),
    )
    op.create_index("idx_permission_id", "ab_permission_view", ["permission_id"])
    op.create_index("idx_view_menu_id", "ab_permission_view", ["view_menu_id"])

    # -- ab_user_role --------------------------------------------------------
    op.create_table(
        "ab_user_role",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("ab_role.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.UniqueConstraint("user_id", "role_id"),
    )

    # -- ab_permission_view_role ---------------------------------------------
    op.create_table(
        "ab_permission_view_role",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "permission_view_id",
            sa.Integer(),
            sa.ForeignKey("ab_permission_view.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("ab_role.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.UniqueConstraint("permission_view_id", "role_id"),
    )
    op.create_index(
        "idx_permission_view_id", "ab_permission_view_role", ["permission_view_id"]
    )
    # FAB canonical name (flask_appbuilder/security/sqla/models.py:94)
    op.create_index("idx_role_id", "ab_permission_view_role", ["role_id"])

    # -- ab_group (FAB Groups) -----------------------------------------------
    op.create_table(
        "ab_group",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "name",
            sa.String(length=100),
            unique=True,
            nullable=False,
        ),
        sa.Column("label", sa.String(length=150)),
        sa.Column("description", sa.String(length=512)),
    )

    # -- ab_user_group -------------------------------------------------------
    op.create_table(
        "ab_user_group",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("ab_group.id", ondelete="CASCADE"),
        ),
        sa.UniqueConstraint("user_id", "group_id"),
    )
    # FAB perf indexes (flask_appbuilder/security/sqla/models.py:252-253).
    op.create_index("idx_user_id", "ab_user_group", ["user_id"])
    op.create_index("idx_user_group_id", "ab_user_group", ["group_id"])

    # -- ab_group_role -------------------------------------------------------
    op.create_table(
        "ab_group_role",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("ab_group.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("ab_role.id", ondelete="CASCADE"),
        ),
        sa.UniqueConstraint("group_id", "role_id"),
    )
    # FAB perf indexes (flask_appbuilder/security/sqla/models.py:269-270).
    op.create_index("idx_group_id", "ab_group_role", ["group_id"])
    op.create_index("idx_group_role_id", "ab_group_role", ["role_id"])

    # -- keyvalue (legacy) ---------------------------------------------------
    op.create_table(
        "keyvalue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )

    # -- annotation_layer ----------------------------------------------------
    op.create_table(
        "annotation_layer",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=250), nullable=True),
        sa.Column("descr", sa.Text(), nullable=True),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- cache_keys ----------------------------------------------------------
    op.create_table(
        "cache_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cache_key", sa.String(length=256), nullable=False),
        sa.Column("cache_timeout", sa.Integer(), nullable=True),
        sa.Column("datasource_uid", sa.String(length=64), nullable=False),
        sa.Column("created_on", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_cache_keys_datasource_uid",
        "cache_keys",
        ["datasource_uid"],
    )

    # -- dynamic_plugin ------------------------------------------------------
    op.create_table(
        "dynamic_plugin",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), unique=True, nullable=False),
        sa.Column("key", sa.Text(), unique=True, nullable=False),
        sa.Column("bundle_url", sa.Text(), unique=True, nullable=False),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- key_value (new) -----------------------------------------------------
    op.create_table(
        "key_value",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("resource", sa.String(length=32), nullable=False),
        sa.Column("value", sa.LargeBinary(length=16777215), nullable=False),
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column("expires_on", sa.DateTime(), nullable=True),
    )
    # 1:1 with upstream 6766938c6065 — the DAO filters on ``expires_on`` for
    # validity checks and cleanup scans.
    op.create_index("ix_key_value_expires_on", "key_value", ["expires_on"])

    # =====================================================================
    # Layer 1: Tables with FKs only to Layer 0
    # =====================================================================

    # -- css_templates -------------------------------------------------------
    op.create_table(
        "css_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_name", sa.String(length=250), nullable=True),
        sa.Column("css", sa.Text(), nullable=True),
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- themes --------------------------------------------------------------
    op.create_table(
        "themes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("theme_name", sa.String(length=250), nullable=True),
        sa.Column("json_data", sa.Text(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column(
            "is_system_default", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("is_system_dark", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )
    op.create_index("idx_theme_is_system_default", "themes", ["is_system_default"])
    op.create_index("idx_theme_is_system_dark", "themes", ["is_system_dark"])

    # -- dbs (Database) ------------------------------------------------------
    op.create_table(
        "dbs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("verbose_name", sa.String(length=250), unique=True, nullable=True),
        sa.Column("database_name", sa.String(length=250), unique=True, nullable=False),
        sa.Column("sqlalchemy_uri", sa.String(length=1024), nullable=False),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column("cache_timeout", sa.Integer(), nullable=True),
        sa.Column("select_as_create_table_as", sa.Boolean(), nullable=True),
        sa.Column("expose_in_sqllab", sa.Boolean(), nullable=True),
        sa.Column(
            "configuration_method",
            sa.String(length=255),
            server_default="sqlalchemy_form",
        ),
        sa.Column("allow_run_async", sa.Boolean(), nullable=True),
        sa.Column("allow_file_upload", sa.Boolean(), nullable=True),
        sa.Column("allow_ctas", sa.Boolean(), nullable=True),
        sa.Column("allow_cvas", sa.Boolean(), nullable=True),
        sa.Column("allow_dml", sa.Boolean(), nullable=True),
        sa.Column("force_ctas_schema", sa.String(length=250), nullable=True),
        sa.Column("extra", sa.Text(), nullable=True),
        sa.Column("encrypted_extra", sa.Text(), nullable=True),
        sa.Column("impersonate_user", sa.Boolean(), nullable=True),
        sa.Column("server_cert", sa.Text(), nullable=True),
        sa.Column(
            "is_managed_externally",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("database_name", name="uq_dbs_database_name"),
    )

    # -- database_user_oauth2_tokens -----------------------------------------
    op.create_table(
        "database_user_oauth2_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "database_id",
            sa.Integer(),
            sa.ForeignKey("dbs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("access_token_expiration", sa.DateTime(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_user_id_database_id",
        "database_user_oauth2_tokens",
        ["user_id", "database_id"],
    )

    # -- logs ----------------------------------------------------------------
    op.create_table(
        "logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action", sa.String(length=512), nullable=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column("dashboard_id", sa.Integer(), nullable=True),
        sa.Column("slice_id", sa.Integer(), nullable=True),
        sa.Column("json", sa.Text(), nullable=True),
        sa.Column("dttm", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("referrer", sa.String(length=1024), nullable=True),
    )

    # -- favstar -------------------------------------------------------------
    op.create_table(
        "favstar",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column("class_name", sa.String(length=50), nullable=True),
        sa.Column("obj_id", sa.Integer(), nullable=True),
        sa.Column("dttm", sa.DateTime(), nullable=True),
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
    )

    # -- annotation ----------------------------------------------------------
    op.create_table(
        "annotation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("start_dttm", sa.DateTime(), nullable=True),
        sa.Column("end_dttm", sa.DateTime(), nullable=True),
        sa.Column(
            "layer_id",
            sa.Integer(),
            sa.ForeignKey("annotation_layer.id"),
            nullable=True,
        ),
        sa.Column("short_descr", sa.String(length=500), nullable=True),
        sa.Column("long_descr", sa.Text(), nullable=True),
        sa.Column("json_metadata", sa.Text(), nullable=True),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ti_dag_state",
        "annotation",
        ["layer_id", "start_dttm", "end_dttm"],
    )

    # -- tag -----------------------------------------------------------------
    op.create_table(
        "tag",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=250), unique=True, nullable=True),
        sa.Column("type", sa.String(length=20), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- tagged_object -------------------------------------------------------
    op.create_table(
        "tagged_object",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tag_id",
            sa.Integer(),
            sa.ForeignKey("tag.id"),
            nullable=True,
        ),
        sa.Column("object_id", sa.Integer(), nullable=True),
        sa.Column("object_type", sa.String(length=20), nullable=True),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "tag_id", "object_id", "object_type", name="uix_tagged_object"
        ),
    )

    # -- user_favorite_tag ---------------------------------------------
    op.create_table(
        "user_favorite_tag",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            sa.Integer(),
            sa.ForeignKey("tag.id"),
            nullable=False,
        ),
    )

    # -- ssh_tunnels ---------------------------------------------------------
    op.create_table(
        "ssh_tunnels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "database_id",
            sa.Integer(),
            sa.ForeignKey("dbs.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("server_address", sa.Text(), nullable=True),
        sa.Column("server_port", sa.Integer(), nullable=True),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column("private_key", sa.Text(), nullable=True),
        sa.Column("private_key_password", sa.Text(), nullable=True),
        sa.Column("extra_json", sa.Text(), nullable=True),
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # =====================================================================
    # Layer 2: Tables (tables/datasets) with FKs to dbs
    # =====================================================================

    # -- tables (SqlaTable / dataset) ----------------------------------------
    op.create_table(
        "tables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("table_name", sa.String(length=250), nullable=False),
        sa.Column("main_dttm_col", sa.String(length=250), nullable=True),
        sa.Column(
            "database_id",
            sa.Integer(),
            sa.ForeignKey("dbs.id"),
            nullable=False,
        ),
        sa.Column("fetch_values_predicate", sa.Text(), nullable=True),
        sa.Column("schema", sa.String(length=255), nullable=True),
        sa.Column("catalog", sa.String(length=256), nullable=True),
        sa.Column("sql", sa.Text(), nullable=True),
        sa.Column("is_sqllab_view", sa.Boolean(), nullable=True),
        sa.Column("template_params", sa.Text(), nullable=True),
        sa.Column("extra", sa.Text(), nullable=True),
        sa.Column("normalize_columns", sa.Boolean(), nullable=True),
        sa.Column("always_filter_main_dttm", sa.Boolean(), nullable=True),
        sa.Column("folders", sa.JSON(), nullable=True),
        # BaseDatasource mixin columns
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_endpoint", sa.Text(), nullable=True),
        sa.Column("is_featured", sa.Boolean(), nullable=True),
        sa.Column("filter_select_enabled", sa.Boolean(), nullable=True),
        sa.Column("offset", sa.Integer(), nullable=True),
        sa.Column("cache_timeout", sa.Integer(), nullable=True),
        sa.Column("params", sa.String(length=1000), nullable=True),
        sa.Column("perm", sa.String(length=1000), nullable=True),
        sa.Column("schema_perm", sa.String(length=1000), nullable=True),
        sa.Column("catalog_perm", sa.String(length=1000), nullable=True),
        sa.Column(
            "is_managed_externally",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("external_url", sa.Text(), nullable=True),
        # ImportExportMixin (UUIDMixin)
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "database_id",
            "catalog",
            "schema",
            "table_name",
            name="uq_tables_database_catalog_schema_table",
        ),
    )

    # -- table_columns -------------------------------------------------------
    op.create_table(
        "table_columns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("verbose_name", sa.String(length=1024), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("advanced_data_type", sa.String(length=255), nullable=True),
        sa.Column("groupby", sa.Boolean(), nullable=True),
        sa.Column("filterable", sa.Boolean(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "table_id",
            sa.Integer(),
            sa.ForeignKey("tables.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_dttm", sa.Boolean(), nullable=True),
        sa.Column("expression", sa.Text(), nullable=True),
        sa.Column("python_date_format", sa.String(length=255), nullable=True),
        sa.Column("extra", sa.Text(), nullable=True),
        # CertificationMixin
        sa.Column("certified_by", sa.Text(), nullable=True),
        sa.Column("certification_details", sa.Text(), nullable=True),
        # ImportExportMixin (UUIDMixin)
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("table_id", "column_name"),
    )

    # -- sql_metrics ---------------------------------------------------------
    op.create_table(
        "sql_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("metric_name", sa.String(length=255), nullable=False),
        sa.Column("verbose_name", sa.String(length=1024), nullable=True),
        sa.Column("metric_type", sa.String(length=32), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("d3format", sa.String(length=128), nullable=True),
        sa.Column("currency", sa.JSON(), nullable=True),
        sa.Column("warning_text", sa.Text(), nullable=True),
        sa.Column(
            "table_id",
            sa.Integer(),
            sa.ForeignKey("tables.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("extra", sa.Text(), nullable=True),
        # CertificationMixin
        sa.Column("certified_by", sa.Text(), nullable=True),
        sa.Column("certification_details", sa.Text(), nullable=True),
        # ImportExportMixin (UUIDMixin)
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("table_id", "metric_name"),
    )

    # -- sqlatable_user (association) ----------------------------------------
    op.create_table(
        "sqlatable_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "table_id",
            sa.Integer(),
            sa.ForeignKey("tables.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    # -- row_level_security_filters ------------------------------------------
    op.create_table(
        "row_level_security_filters",
        sa.Column("id", sa.Integer(), primary_key=True),
        # NOT NULL — squash reflects the FINAL upstream state (migration
        # f3afaf1f11f0 added the column then ``SET NOT NULL``); the ORM model
        # declares ``nullable=False`` too.
        sa.Column("name", sa.String(length=255), unique=True, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("filter_type", sa.String(length=50), nullable=True),
        sa.Column("group_key", sa.String(length=255), nullable=True),
        sa.Column("clause", sa.Text(), nullable=False),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- rls_filter_roles (association) --------------------------------------
    op.create_table(
        "rls_filter_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("ab_role.id"),
            nullable=False,
        ),
        sa.Column(
            "rls_filter_id",
            sa.Integer(),
            sa.ForeignKey(
                "row_level_security_filters.id",
            ),
            nullable=True,
        ),
    )

    # -- rls_filter_tables (association) -----
    op.create_table(
        "rls_filter_tables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "table_id",
            sa.Integer(),
            sa.ForeignKey("tables.id"),
            nullable=True,
        ),
        sa.Column(
            "rls_filter_id",
            sa.Integer(),
            sa.ForeignKey(
                "row_level_security_filters.id",
            ),
            nullable=True,
        ),
    )

    # =====================================================================
    # Layer 3: Slices (charts) -- depend on ab_user
    # =====================================================================

    # -- slices --------------------------------------------------------------
    op.create_table(
        "slices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slice_name", sa.String(length=250), nullable=True),
        sa.Column("datasource_id", sa.Integer(), nullable=True),
        sa.Column("datasource_type", sa.String(length=200), nullable=True),
        sa.Column("datasource_name", sa.String(length=2000), nullable=True),
        sa.Column("viz_type", sa.String(length=250), nullable=True),
        sa.Column("params", sa.Text(), nullable=True),
        sa.Column("query_context", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("cache_timeout", sa.Integer(), nullable=True),
        sa.Column("perm", sa.String(length=1000), nullable=True),
        sa.Column("schema_perm", sa.String(length=1000), nullable=True),
        sa.Column("catalog_perm", sa.String(length=1000), nullable=True),
        sa.Column("last_saved_at", sa.DateTime(), nullable=True),
        sa.Column(
            "last_saved_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column("certified_by", sa.Text(), nullable=True),
        sa.Column("certification_details", sa.Text(), nullable=True),
        sa.Column(
            "is_managed_externally",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("external_url", sa.Text(), nullable=True),
        # ImportExportMixin (UUIDMixin)
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- slice_user (association) --------------------------------------------
    op.create_table(
        "slice_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "slice_id",
            sa.Integer(),
            sa.ForeignKey("slices.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    # =====================================================================
    # Layer 4: Dashboards -- depend on themes
    # =====================================================================

    # -- dashboards ----------------------------------------------------------
    op.create_table(
        "dashboards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dashboard_title", sa.String(length=500), nullable=True),
        sa.Column("position_json", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("css", sa.Text(), nullable=True),
        sa.Column(
            "theme_id",
            sa.Integer(),
            sa.ForeignKey("themes.id"),
            nullable=True,
        ),
        sa.Column("certified_by", sa.Text(), nullable=True),
        sa.Column("certification_details", sa.Text(), nullable=True),
        sa.Column("json_metadata", sa.Text(), nullable=True),
        sa.Column("slug", sa.String(length=255), unique=True, nullable=True),
        sa.Column("published", sa.Boolean(), nullable=True),
        sa.Column(
            "is_managed_externally",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("external_url", sa.Text(), nullable=True),
        # ImportExportMixin (UUIDMixin)
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- dashboard_slices (association) --------------------------------------
    op.create_table(
        "dashboard_slices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dashboard_id",
            sa.Integer(),
            sa.ForeignKey("dashboards.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "slice_id",
            sa.Integer(),
            sa.ForeignKey("slices.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.UniqueConstraint("dashboard_id", "slice_id"),
    )

    # -- dashboard_user (association) ----------------------------------------
    op.create_table(
        "dashboard_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "dashboard_id",
            sa.Integer(),
            sa.ForeignKey("dashboards.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    # -- dashboard_roles (association) ---------------------------------------
    op.create_table(
        "dashboard_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dashboard_id",
            sa.Integer(),
            sa.ForeignKey("dashboards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("ab_role.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    # -- embedded_dashboards -------------------------------------------------
    op.create_table(
        "embedded_dashboards",
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            primary_key=True,
        ),
        sa.Column("allow_domain_list", sa.Text(), nullable=True),
        sa.Column(
            "dashboard_id",
            sa.Integer(),
            sa.ForeignKey("dashboards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # =====================================================================
    # Layer 5: Tables that depend on dashboards/slices/dbs
    # =====================================================================

    # -- user_attribute (deferred FK to dashboards) --------------------------
    op.create_table(
        "user_attribute",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "welcome_dashboard_id",
            sa.Integer(),
            sa.ForeignKey("dashboards.id"),
            nullable=True,
        ),
        sa.Column("avatar_url", sa.String(length=100), nullable=True),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- report_schedule -----------------------------------------------------
    op.create_table(
        "report_schedule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("context_markdown", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column("crontab", sa.String(length=1000), nullable=False),
        sa.Column(
            "creation_method",
            sa.String(length=255),
            server_default="alerts_reports",
        ),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("report_format", sa.String(length=50), nullable=True),
        sa.Column("sql", sa.Text(), nullable=True),
        sa.Column(
            "chart_id",
            sa.Integer(),
            sa.ForeignKey("slices.id"),
            nullable=True,
        ),
        sa.Column(
            "dashboard_id",
            sa.Integer(),
            sa.ForeignKey("dashboards.id"),
            nullable=True,
        ),
        sa.Column(
            "database_id",
            sa.Integer(),
            sa.ForeignKey("dbs.id"),
            nullable=True,
        ),
        sa.Column("last_eval_dttm", sa.DateTime(), nullable=True),
        sa.Column("last_state", sa.String(length=50), nullable=True),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("last_value_row_json", sa.Text(), nullable=True),
        sa.Column("validator_type", sa.String(length=100), nullable=True),
        sa.Column("validator_config_json", sa.Text(), nullable=True),
        sa.Column("log_retention", sa.Integer(), nullable=True),
        sa.Column("grace_period", sa.Integer(), nullable=True),
        sa.Column("working_timeout", sa.Integer(), nullable=True),
        sa.Column("force_screenshot", sa.Boolean(), nullable=True),
        sa.Column("custom_width", sa.Integer(), nullable=True),
        sa.Column("custom_height", sa.Integer(), nullable=True),
        sa.Column("email_subject", sa.String(length=255), nullable=True),
        # ExtraJSONMixin
        sa.Column("extra_json", sa.Text(), nullable=True),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("name", "type", name="uq_report_schedule_name_type"),
    )
    op.create_index(
        "ix_report_schedule_active",
        "report_schedule",
        ["active"],
    )
    # 1:1 with upstream 3317e9248280 — distinguishes alerts vs reports.
    op.create_index(
        "ix_creation_method",
        "report_schedule",
        ["creation_method"],
    )

    # -- report_schedule_user (association) ----------------------------------
    op.create_table(
        "report_schedule_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey(
                "ab_user.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "report_schedule_id",
            sa.Integer(),
            sa.ForeignKey(
                "report_schedule.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id",
            "report_schedule_id",
        ),
    )

    # -- report_recipient ---------------------------------------------------
    op.create_table(
        "report_recipient",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("recipient_config_json", sa.Text(), nullable=True),
        sa.Column(
            "report_schedule_id",
            sa.Integer(),
            sa.ForeignKey("report_schedule.id"),
            nullable=False,
        ),
        # AuditMixinNullable
        sa.Column(
            "created_on",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column(
            "changed_on",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_report_recipient_report_schedule_id",
        "report_recipient",
        ["report_schedule_id"],
    )

    # -- report_execution_log ---------------
    op.create_table(
        "report_execution_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            nullable=True,
        ),
        sa.Column("scheduled_dttm", sa.DateTime(), nullable=False),
        sa.Column("start_dttm", sa.DateTime(), nullable=True),
        sa.Column("end_dttm", sa.DateTime(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("value_row_json", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=50), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "report_schedule_id",
            sa.Integer(),
            sa.ForeignKey("report_schedule.id"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_report_execution_log_report_schedule_id",
        "report_execution_log",
        ["report_schedule_id"],
    )
    op.create_index(
        "ix_report_execution_log_start_dttm",
        "report_execution_log",
        ["start_dttm"],
    )

    # =====================================================================
    # Layer 6: SQL Lab tables -- depend on dbs and ab_user
    # =====================================================================

    # -- query ---------------------------------------------------------------
    op.create_table(
        "query",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.String(length=11), unique=True, nullable=False),
        sa.Column(
            "database_id",
            sa.Integer(),
            sa.ForeignKey("dbs.id"),
            nullable=False,
        ),
        sa.Column("tmp_table_name", sa.String(length=256), nullable=True),
        sa.Column("tmp_schema_name", sa.String(length=256), nullable=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("tab_name", sa.String(length=256), nullable=True),
        sa.Column("sql_editor_id", sa.String(length=256), nullable=True),
        sa.Column("schema", sa.String(length=256), nullable=True),
        sa.Column("catalog", sa.String(length=256), nullable=True),
        sa.Column("sql", sa.Text(), nullable=True),
        sa.Column("select_sql", sa.Text(), nullable=True),
        sa.Column("executed_sql", sa.Text(), nullable=True),
        sa.Column("limit", sa.Integer(), nullable=True),
        sa.Column(
            "limiting_factor",
            sa.String(length=20),
            server_default="UNKNOWN",
            nullable=True,
        ),
        sa.Column("select_as_cta", sa.Boolean(), nullable=True),
        sa.Column("select_as_cta_used", sa.Boolean(), nullable=True),
        sa.Column("ctas_method", sa.String(length=16), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("results_key", sa.String(length=64), nullable=True),
        sa.Column("start_time", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column(
            "start_running_time",
            sa.Numeric(precision=20, scale=6),
            nullable=True,
        ),
        sa.Column("end_time", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column(
            "end_result_backend_time",
            sa.Numeric(precision=20, scale=6),
            nullable=True,
        ),
        sa.Column("tracking_url", sa.Text(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        # ExtraJSONMixin
        sa.Column("extra_json", sa.Text(), nullable=True),
    )
    op.create_index(
        "ti_user_id_changed_on",
        "query",
        ["user_id", "changed_on"],
    )
    op.create_index(
        "ix_query_sql_editor_id",
        "query",
        ["sql_editor_id"],
    )
    op.create_index(
        "ix_query_results_key",
        "query",
        ["results_key"],
    )

    # -- saved_query ---------------------------------------------------------
    op.create_table(
        "saved_query",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "db_id",
            sa.Integer(),
            sa.ForeignKey("dbs.id"),
            nullable=True,
        ),
        sa.Column("schema", sa.String(length=128), nullable=True),
        sa.Column("catalog", sa.String(length=256), nullable=True),
        sa.Column("label", sa.String(length=256), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sql", sa.Text(), nullable=True),
        sa.Column("template_parameters", sa.Text(), nullable=True),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("last_run", sa.DateTime(), nullable=True),
        # ExtraJSONMixin
        sa.Column("extra_json", sa.Text(), nullable=True),
        # ImportExportMixin (UUIDMixin)
        sa.Column(
            "uuid",
            sa.LargeBinary(length=16),
            unique=True,
            nullable=True,
        ),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- tab_state -----------------------------------------------------------
    op.create_table(
        "tab_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column("label", sa.String(length=256), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column(
            "database_id",
            sa.Integer(),
            sa.ForeignKey("dbs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("schema", sa.String(length=256), nullable=True),
        sa.Column("catalog", sa.String(length=256), nullable=True),
        sa.Column("sql", sa.Text(), nullable=True),
        sa.Column("query_limit", sa.Integer(), nullable=True),
        sa.Column(
            "latest_query_id",
            sa.String(length=11),
            sa.ForeignKey("query.client_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("autorun", sa.Boolean(), nullable=True),
        sa.Column("template_params", sa.Text(), nullable=True),
        sa.Column("hide_left_bar", sa.Boolean(), nullable=True),
        sa.Column(
            "saved_query_id",
            sa.Integer(),
            sa.ForeignKey("saved_query.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # ExtraJSONMixin
        sa.Column("extra_json", sa.Text(), nullable=True),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )

    # -- table_schema --------------------------------------------------------
    op.create_table(
        "table_schema",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "tab_state_id",
            sa.Integer(),
            sa.ForeignKey("tab_state.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "database_id",
            sa.Integer(),
            sa.ForeignKey("dbs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema", sa.String(length=256), nullable=True),
        sa.Column("catalog", sa.String(length=256), nullable=True),
        sa.Column("table", sa.String(length=256), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("expanded", sa.Boolean(), nullable=True),
        # ExtraJSONMixin
        sa.Column("extra_json", sa.Text(), nullable=True),
        # AuditMixinNullable
        sa.Column("created_on", sa.DateTime(), nullable=True),
        sa.Column("changed_on", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
        sa.Column(
            "changed_by_fk",
            sa.Integer(),
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Drop all tables in reverse dependency order.
    op.drop_table("table_schema")
    op.drop_table("tab_state")
    op.drop_table("saved_query")
    op.drop_index("ix_query_results_key", table_name="query")
    op.drop_index("ix_query_sql_editor_id", table_name="query")
    op.drop_index("ti_user_id_changed_on", table_name="query")
    op.drop_table("query")
    op.drop_table("report_execution_log")
    op.drop_table("report_recipient")
    op.drop_table("report_schedule_user")
    op.drop_index("ix_creation_method", table_name="report_schedule")
    op.drop_index("ix_report_schedule_active", table_name="report_schedule")
    op.drop_table("report_schedule")
    op.drop_table("user_attribute")
    op.drop_table("embedded_dashboards")
    op.drop_table("dashboard_roles")
    op.drop_table("dashboard_user")
    op.drop_table("dashboard_slices")
    op.drop_table("dashboards")
    op.drop_table("slice_user")
    op.drop_table("slices")
    op.drop_table("rls_filter_tables")
    op.drop_table("rls_filter_roles")
    op.drop_table("row_level_security_filters")
    op.drop_table("sqlatable_user")
    op.drop_table("sql_metrics")
    op.drop_table("table_columns")
    op.drop_table("tables")
    op.drop_table("ssh_tunnels")
    op.drop_table("user_favorite_tag")
    op.drop_table("tagged_object")
    op.drop_table("tag")
    op.drop_index("ti_dag_state", table_name="annotation")
    op.drop_table("annotation")
    op.drop_table("favstar")
    op.drop_table("logs")
    op.drop_index("idx_user_id_database_id", table_name="database_user_oauth2_tokens")
    op.drop_table("database_user_oauth2_tokens")
    op.drop_table("dbs")
    op.drop_index("idx_theme_is_system_dark", table_name="themes")
    op.drop_index("idx_theme_is_system_default", table_name="themes")
    op.drop_table("themes")
    op.drop_table("css_templates")
    op.drop_table("key_value")
    op.drop_table("dynamic_plugin")
    op.drop_index("ix_cache_keys_datasource_uid", table_name="cache_keys")
    op.drop_table("cache_keys")
    op.drop_table("annotation_layer")
    op.drop_table("keyvalue")
    op.drop_table("ab_permission_view_role")
    op.drop_table("ab_group_role")
    op.drop_table("ab_user_group")
    op.drop_table("ab_user_role")
    op.drop_table("ab_permission_view")
    op.drop_table("ab_register_user")
    op.drop_table("ab_user")
    op.drop_table("ab_group")
    op.drop_table("ab_role")
    op.drop_table("ab_view_menu")
    op.drop_table("ab_permission")
