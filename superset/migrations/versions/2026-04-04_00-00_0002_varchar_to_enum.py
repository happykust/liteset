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
"""Convert VARCHAR columns to native PostgreSQL ENUM types.

The initial squashed migration created query.limiting_factor and
tag.type as VARCHAR for compatibility.  The ORM models use
SQLAlchemy Enum() which expects native PostgreSQL ENUM types
when running under asyncpg.  This migration creates the ENUM types
and converts the columns.

Revision ID: a1b2c3d4e5f6
Revises: c233f5365c9e
Create Date: 2026-04-04 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "c233f5365c9e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # --- query.limiting_factor: VARCHAR(20) → ENUM limitingfactor ---
    op.execute(
        "CREATE TYPE limitingfactor AS ENUM "
        "('UNKNOWN', 'LIMITED', 'QUERY', 'QUERY_AND_DROPDOWN', "
        "'NOT_LIMITED', 'DROPDOWN')"
    )
    # Drop default before type change, re-add after
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

    # --- tag.type: VARCHAR(20) → ENUM tagtype ---
    op.execute(
        "CREATE TYPE tagtype AS ENUM ('custom', 'type', 'owner', 'favorited_by')"
    )
    op.execute("ALTER TABLE tag ALTER COLUMN type TYPE tagtype USING type::tagtype")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # --- tag.type: ENUM → VARCHAR(20) ---
    op.execute("ALTER TABLE tag ALTER COLUMN type TYPE VARCHAR(20) USING type::text")
    op.execute("DROP TYPE IF EXISTS tagtype")

    # --- query.limiting_factor: ENUM → VARCHAR(20) ---
    op.execute(
        "ALTER TABLE query "
        "ALTER COLUMN limiting_factor TYPE VARCHAR(20) "
        "USING limiting_factor::text"
    )
    op.execute("DROP TYPE IF EXISTS limitingfactor")
