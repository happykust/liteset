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
"""Convert query.limiting_factor and tag.type back to VARCHAR.

Revision ``a1b2c3d4e5f6`` turned both columns into native PostgreSQL ENUMs on
the premise that the ORM required them.  It does not: ``Query.limiting_factor``
(``superset/models/sql_lab.py``) and ``Tag.type`` (``superset/models/tags.py``)
are declared ``Enum(..., native_enum=False)``, which keeps the column VARCHAR
and sends plain text.  Revision ``b2c3d4e5f6a7`` already settled on that
position for ``tagged_object.object_type``; this brings the remaining two
columns in line so the schema and the models agree.

Idempotent by construction: each column is only touched when it is still a
native enum, so this is a no-op on databases that never ran ``a1b2c3d4e5f6``
(non-PostgreSQL backends, or fresh installs once it is retired).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-29 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def _column_udt(table: str, column: str) -> str | None:
    """Return the underlying type name of a column, or ``None`` if absent."""
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        """
        SELECT udt_name
        FROM information_schema.columns
        WHERE table_name = %(table)s AND column_name = %(column)s
        """,
        {"table": table, "column": column},
    ).first()
    return row[0] if row else None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # --- query.limiting_factor: ENUM limitingfactor → VARCHAR(20) ---
    if _column_udt("query", "limiting_factor") == "limitingfactor":
        op.execute("ALTER TABLE query ALTER COLUMN limiting_factor DROP DEFAULT")
        op.execute(
            "ALTER TABLE query "
            "ALTER COLUMN limiting_factor TYPE VARCHAR(20) "
            "USING limiting_factor::text"
        )
        op.execute(
            "ALTER TABLE query ALTER COLUMN limiting_factor SET DEFAULT 'UNKNOWN'"
        )
    op.execute("DROP TYPE IF EXISTS limitingfactor")

    # --- tag.type: ENUM tagtype → VARCHAR(20) ---
    if _column_udt("tag", "type") == "tagtype":
        op.execute(
            "ALTER TABLE tag ALTER COLUMN type TYPE VARCHAR(20) USING type::text"
        )
    op.execute("DROP TYPE IF EXISTS tagtype")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Recreating the types is guarded the same way ``a1b2c3d4e5f6`` is: a
    # leftover type from an older Apache Superset schema must not abort this.
    op.execute(
        """
        DO $$
        BEGIN
            CREATE TYPE limitingfactor AS ENUM (
                'UNKNOWN', 'LIMITED', 'QUERY', 'QUERY_AND_DROPDOWN',
                'NOT_LIMITED', 'DROPDOWN'
            );
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END
        $$;
        """
    )
    op.execute("ALTER TABLE query ALTER COLUMN limiting_factor DROP DEFAULT")
    op.execute(
        "ALTER TABLE query "
        "ALTER COLUMN limiting_factor TYPE limitingfactor "
        "USING limiting_factor::limitingfactor"
    )
    op.execute(
        "ALTER TABLE query "
        "ALTER COLUMN limiting_factor SET DEFAULT 'UNKNOWN'::limitingfactor"
    )

    op.execute(
        """
        DO $$
        BEGIN
            CREATE TYPE tagtype AS ENUM (
                'custom', 'type', 'owner', 'favorited_by'
            );
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END
        $$;
        """
    )
    op.execute("ALTER TABLE tag ALTER COLUMN type TYPE tagtype USING type::tagtype")
