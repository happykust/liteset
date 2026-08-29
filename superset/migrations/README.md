<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# Liteset metadata-DB migrations

Liteset's Alembic chain is intentionally short: it begins with a
**squashed initial revision** (`c233f5365c9e`, identical to the head
of the upstream Apache Superset 6.0 chain) and adds a handful of
small, dialect-aware deltas on top.

| File | Revision | Notes |
|---|---|---|
| `2026-03-28_0001_initial_schema.py` | `c233f5365c9e` | Full snapshot of the Superset 6.0 schema (50 tables). |
| `2026-04-04_0002_varchar_to_enum.py` | `a1b2c3d4e5f6` | PostgreSQL-only: convert `query.limiting_factor` and `tag.type` to native `ENUM`s. No-op on MySQL/SQLite. **Superseded by `c3d4e5f6a7b8`** — kept in the chain for history only. Its `CREATE TYPE` calls are guarded: `tagtype` survives on metadata DBs that passed through Apache Superset ≤ 2.1, and PostgreSQL has no `CREATE TYPE IF NOT EXISTS`. |
| `2026-05-04_0003_ddl_drift_fixes.py` | `b2c3d4e5f6a7` | DDL drift corrections (NOT NULL / UNIQUE / index restorations; `tagged_object.object_type` stays VARCHAR, matching upstream `07f9a902af1b`). |
| `2026-08-29_0004_enum_back_to_varchar.py` | `c3d4e5f6a7b8` | PostgreSQL-only: convert `query.limiting_factor` and `tag.type` back to `VARCHAR` and drop the enum types, so the schema matches the models — both are declared `Enum(..., native_enum=False)`. Idempotent. |

## Driver split

* **Runtime:** `postgresql+asyncpg://` (or `mysql+asyncmy`,
  `sqlite+aiosqlite`).
* **Migrations:** the same DSN with the async driver substituted for
  its sync counterpart (`psycopg2`, `pymysql`, `sqlite`).  See
  `env.py:_ASYNC_TO_SYNC_DRIVERS`.

This split is deliberate — Alembic's transactional DDL needs a sync
driver, while every runtime path (DAOs, controllers, websocket) runs
on top of `AsyncSession`.

## Migrating from Apache Superset

Liteset can be pointed at an existing Apache Superset metadata DB,
**but only when that DB is at the upstream head revision
`c233f5365c9e`** (the 2025-08-05 release).  Alembic does not know
about the 339 intermediate revisions in
`superset_old/migrations/versions/`; if your DB is at any earlier
revision you must first run the upstream chain to bring it up to
`c233f5365c9e`, then point Liteset's `alembic upgrade head` at it.

Concretely:

```bash
# 1. Stop your Liteset / Superset processes.
# 2. Using a *legacy* Apache Superset 6.0 install:
SUPERSET_SQLALCHEMY_DATABASE_URI=postgresql+psycopg2://… \
    superset db upgrade

# 3. Switch to Liteset and run the short chain:
SUPERSET_SQLALCHEMY_DATABASE_URI=postgresql+psycopg2://… \
    alembic --config superset/migrations/alembic.ini upgrade head
```

After step 3 the DB sits at `b2c3d4e5f6a7` (or whichever the latest
Liteset head is) and Liteset can connect with the async driver.

## Encrypted columns

`Database.password / encrypted_extra / server_cert`,
`DatabaseUserOAuth2Tokens.access_token / refresh_token` and every
`ssh_tunnels` credential column are wrapped in
`sqlalchemy_utils.EncryptedType` via
`superset.utils.encrypt.EncryptedFieldFactory`.  This must match the
key used by the original Apache Superset deployment (`SECRET_KEY` in
`superset_config.py` / `SupersetSettings.secret_key`).  If you rotate
the key, run the bundled `SecretsMigrator` (CLI: `superset
re-encrypt-secrets`) **before** restarting under the new key.

## Adding a new migration

```bash
SUPERSET_SQLALCHEMY_DATABASE_URI=postgresql+psycopg2://… \
    alembic --config superset/migrations/alembic.ini \
    revision -m "your message" --autogenerate
```

Autogenerate diffs against `superset.models.helpers.Base.metadata` —
i.e. it reads every `Column` / `Index` / `UniqueConstraint` declared
on the ORM models in `superset/models/`.  Always inspect the result;
autogenerate cannot detect default-value changes or column comments.

## Notes for MySQL / SQLite users

* The `0002_varchar_to_enum` revision is gated by
  `op.get_bind().dialect.name == "postgresql"` and is a no-op on
  MySQL / SQLite.
* The `0003_ddl_drift_fixes` revision uses `op.batch_alter_table`
  where required for SQLite (`ALTER COLUMN` is unsupported).
* Several `op.alter_column(... nullable=False)` calls are wrapped in
  a `try/except` so legacy databases with rows that violate the new
  constraint can still apply the migration; the ORM enforces the
  constraint on subsequent writes.
