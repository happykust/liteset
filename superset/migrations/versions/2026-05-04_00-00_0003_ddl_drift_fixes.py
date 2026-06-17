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
"""DDL drift fixes — close the gap between Liteset model declarations
and the upstream Apache Superset 6.0 / ``c233f5365c9e`` schema.

Audit reference: ``docs/audit_2026-05-04/05-models-dao-migrations.md``.

Each ``op.alter_column`` / ``op.create_index`` block re-instates a
constraint or column attribute that the squashed
``2026-03-28_0001_initial_schema.py`` either created already (no-op
on a freshly-bootstrapped DB) or omitted (corrected here).  All
operations are idempotent: any existing index or constraint with the
same name is dropped first when the upgrade rebuilds it.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _run_in_savepoint(action: "callable[[], None]") -> None:
    """Run a DDL action inside a SAVEPOINT so PostgreSQL doesn't poison the
    outer transaction when the action fails.

    On PostgreSQL, a failed statement aborts the whole transaction until
    ROLLBACK; subsequent statements raise ``InFailedSqlTransaction``.
    Wrapping each idempotent DDL in ``begin_nested()`` lets us rollback
    only the inner SAVEPOINT, leaving the outer Alembic transaction
    healthy for the next migration step.

    On MySQL/SQLite ``begin_nested()`` is also safe (SAVEPOINTs are
    supported on InnoDB and modern SQLite); we use it unconditionally.
    """
    bind = op.get_bind()
    savepoint = bind.begin_nested()
    try:
        action()
        savepoint.commit()
    except Exception:  # noqa: BLE001
        savepoint.rollback()


def _safe_alter_column_nullable(
    table: str,
    column: str,
    *,
    nullable: bool,
    existing_type: sa.types.TypeEngine,
) -> None:
    """``op.alter_column`` wrapped in a SAVEPOINT so MySQL/SQLite
    (which can't ALTER NOT NULL when the column already has rows
    violating the constraint) — and PostgreSQL (which aborts the
    transaction on any failure) — just no-op cleanly.
    """
    _run_in_savepoint(
        lambda: op.alter_column(
            table,
            column,
            nullable=nullable,
            existing_type=existing_type,
        )
    )


def _safe_create_index(
    name: str, table: str, columns: list[str], *, unique: bool = False
) -> None:
    """Create an index, ignoring the case where it already exists."""
    _run_in_savepoint(lambda: op.create_index(name, table, columns, unique=unique))


def _safe_drop_index(name: str, table: str) -> None:
    _run_in_savepoint(lambda: op.drop_index(name, table_name=table))


def _safe_create_unique_constraint(
    name: str | None, table: str, columns: list[str]
) -> None:
    _run_in_savepoint(lambda: op.create_unique_constraint(name, table, columns))


def _safe_drop_constraint(name: str, table: str, type_: str | None = None) -> None:
    def _do() -> None:
        if type_:
            op.drop_constraint(name, table, type_=type_)
        else:
            op.drop_constraint(name, table)

    _run_in_savepoint(_do)


def upgrade() -> None:
    _safe_alter_column_nullable(
        "annotation",
        "layer_id",
        nullable=False,
        existing_type=sa.Integer(),
    )

    # ------------------------------------------------------------------
    # tagged_object.object_type stays VARCHAR.
    #
    # Apache Superset's migration ``07f9a902af1b`` (2023-03-29) dropped
    # the ``objecttypes`` PG ENUM and converted the column back to
    # VARCHAR.  We match that production state.  The model uses
    # ``Enum(ObjectType, native_enum=False)`` so asyncpg sends values
    # as plain text instead of ``$1::objecttype`` casts that would fail
    # on installations where the ENUM type doesn't exist.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # report_schedule -- restore NOT NULLs / defaults
    # (see audit §"report_schedule schema drift")
    # ------------------------------------------------------------------
    _safe_alter_column_nullable(
        "report_schedule", "type", nullable=False, existing_type=sa.String(length=50)
    )
    _safe_alter_column_nullable(
        "report_schedule",
        "crontab",
        nullable=False,
        existing_type=sa.String(length=1000),
    )
    _safe_alter_column_nullable(
        "report_schedule",
        "timezone",
        nullable=False,
        existing_type=sa.String(length=100),
    )

    # ------------------------------------------------------------------
    # report_recipient -- type NOT NULL + index on FK
    # ------------------------------------------------------------------
    _safe_alter_column_nullable(
        "report_recipient", "type", nullable=False, existing_type=sa.String(length=50)
    )
    # The squash already creates ``ix_report_recipient_report_schedule_id``
    # so this is a no-op on a fresh DB but ensures it exists when an old
    # Apache Superset DB lacking the index is mounted.
    _safe_create_index(
        "ix_report_recipient_report_schedule_id",
        "report_recipient",
        ["report_schedule_id"],
    )

    # ------------------------------------------------------------------
    # report_execution_log -- NOT NULLs + indexes
    # ------------------------------------------------------------------
    _safe_alter_column_nullable(
        "report_execution_log",
        "scheduled_dttm",
        nullable=False,
        existing_type=sa.DateTime(),
    )
    _safe_alter_column_nullable(
        "report_execution_log",
        "state",
        nullable=False,
        existing_type=sa.String(length=50),
    )
    _safe_create_index(
        "ix_report_execution_log_report_schedule_id",
        "report_execution_log",
        ["report_schedule_id"],
    )
    _safe_create_index(
        "ix_report_execution_log_start_dttm",
        "report_execution_log",
        ["start_dttm"],
    )

    # ------------------------------------------------------------------
    # report_schedule_user -- (user_id, report_schedule_id) UNIQUE
    # ------------------------------------------------------------------
    _safe_alter_column_nullable(
        "report_schedule_user",
        "user_id",
        nullable=False,
        existing_type=sa.Integer(),
    )
    _safe_alter_column_nullable(
        "report_schedule_user",
        "report_schedule_id",
        nullable=False,
        existing_type=sa.Integer(),
    )
    _safe_create_unique_constraint(
        "uq_report_schedule_user_user_id_report_schedule_id",
        "report_schedule_user",
        ["user_id", "report_schedule_id"],
    )

    # ------------------------------------------------------------------
    # FAB association tables -- restore UniqueConstraint on the
    # (a_id, b_id) pair.  Mirrors
    # flask_appbuilder/security/sqla/models.py.  ``ondelete="CASCADE"``
    # is harder to alter portably (PG / MySQL behave differently) so
    # we leave that to a one-shot migration users can run when they
    # want to enforce CASCADE -- the ORM never relies on it because
    # the corresponding ``relationship(passive_deletes=True)`` calls
    # only matter for in-Python deletes.
    # ------------------------------------------------------------------
    _safe_create_unique_constraint(
        "uq_ab_user_role_user_id_role_id",
        "ab_user_role",
        ["user_id", "role_id"],
    )
    _safe_create_unique_constraint(
        "uq_ab_permission_view_role_pv_id_role_id",
        "ab_permission_view_role",
        ["permission_view_id", "role_id"],
    )
    _safe_create_unique_constraint(
        "uq_ab_user_group_user_id_group_id",
        "ab_user_group",
        ["user_id", "group_id"],
    )
    _safe_create_unique_constraint(
        "uq_ab_group_role_group_id_role_id",
        "ab_group_role",
        ["group_id", "role_id"],
    )

    # ab_permission_view -- (permission_id, view_menu_id) UNIQUE +
    # supporting indexes (matches FAB's ``__table_args__``).
    _safe_create_unique_constraint(
        "uq_ab_permission_view_permission_view_menu",
        "ab_permission_view",
        ["permission_id", "view_menu_id"],
    )
    _safe_create_index("idx_permission_id", "ab_permission_view", ["permission_id"])
    _safe_create_index("idx_view_menu_id", "ab_permission_view", ["view_menu_id"])

    # ------------------------------------------------------------------
    # ab_user -- self-referential audit columns (matches FAB schema).
    # ------------------------------------------------------------------
    _add_user_audit_fk_if_missing("created_by_fk")
    _add_user_audit_fk_if_missing("changed_by_fk")


def _add_user_audit_fk_if_missing(column: str) -> None:
    """Add an ``ab_user.<column> FK -> ab_user.id`` if absent.

    Idempotent on every backend: skips when the column already exists
    (e.g. on a fresh DB the squash has already created it).  Wrapped in
    a SAVEPOINT so a single failed reflection or DDL call doesn't poison
    the whole migration's PostgreSQL transaction.
    """

    def _do() -> None:
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        cols = {c["name"] for c in inspector.get_columns("ab_user")}
        if column in cols:
            return
        fk_name = f"fk_ab_user_{column}_ab_user_id"
        with op.batch_alter_table("ab_user") as batch:
            batch.add_column(
                sa.Column(
                    column,
                    sa.Integer(),
                    sa.ForeignKey("ab_user.id", name=fk_name),
                    nullable=True,
                )
            )

    _run_in_savepoint(_do)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # FAB ab_user audit columns
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("ab_user")}
    for column in ("changed_by_fk", "created_by_fk"):
        if column in cols:
            try:
                with op.batch_alter_table("ab_user") as batch:
                    batch.drop_column(column)
            except Exception:  # noqa: BLE001, S110
                pass

    _safe_drop_index("idx_view_menu_id", "ab_permission_view")
    _safe_drop_index("idx_permission_id", "ab_permission_view")
    _safe_drop_constraint(
        "uq_ab_permission_view_permission_view_menu",
        "ab_permission_view",
        type_="unique",
    )

    # FAB association table unique constraints
    _safe_drop_constraint(
        "uq_ab_group_role_group_id_role_id", "ab_group_role", type_="unique"
    )
    _safe_drop_constraint(
        "uq_ab_user_group_user_id_group_id", "ab_user_group", type_="unique"
    )
    _safe_drop_constraint(
        "uq_ab_permission_view_role_pv_id_role_id",
        "ab_permission_view_role",
        type_="unique",
    )
    _safe_drop_constraint(
        "uq_ab_user_role_user_id_role_id", "ab_user_role", type_="unique"
    )

    _safe_drop_constraint(
        "uq_report_schedule_user_user_id_report_schedule_id",
        "report_schedule_user",
        type_="unique",
    )

    _safe_drop_index("ix_report_execution_log_start_dttm", "report_execution_log")
    _safe_drop_index(
        "ix_report_execution_log_report_schedule_id", "report_execution_log"
    )

    _safe_drop_index("ix_report_recipient_report_schedule_id", "report_recipient")

    # tagged_object.object_type — revert to VARCHAR on PG
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE tagged_object "
            "ALTER COLUMN object_type TYPE VARCHAR(20) "
            "USING object_type::text"
        )
        op.execute("DROP TYPE IF EXISTS objecttype")
