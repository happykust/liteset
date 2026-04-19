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

# Liteset

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/license/apache-2-0)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Litestar](https://img.shields.io/badge/Litestar-2.15+-7c3aed.svg)](https://litestar.dev/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-red.svg)](https://www.sqlalchemy.org/)
[![Based on Apache Superset](https://img.shields.io/badge/based%20on-Apache%20Superset%206.0.0-blue.svg)](https://github.com/apache/superset)

**Liteset is an async port of Apache Superset, rewritten from Flask/WSGI onto Litestar/ASGI.**

The project preserves full backward compatibility with existing Apache Superset installations: the metadata database schema, the HTTP API, the WebSocket contract and the frontend stay unchanged. Stop Superset, install Liteset on top of the same database, and keep working with the same dashboards, datasets, users and roles.

---

## Table of contents

- [Motivation](#motivation)
- [Target architecture](#target-architecture)
- [Technology stack](#technology-stack)
- [Compatibility guarantees](#compatibility-guarantees)
- [Project layout](#project-layout)
- [Installation and running](#installation-and-running)
- [License](#license)

---

## Motivation

Historically Apache Superset is built on Flask/WSGI and runs under Gunicorn with pre-forked processes. This model has three fundamental limitations:

1. **Blocking I/O.** During long-running queries against analytical databases the worker thread sits idle waiting for a response and does not serve other requests.
2. **High memory footprint.** Every worker process copies the whole application and maintains its own metadata-database connection pool.
3. **Limited concurrency.** The number of concurrent requests is hard-capped by processes × threads.

Liteset removes these bottlenecks by moving the entire web layer to the async ASGI model. The expected outcome is a 2–3× RPS gain on IO-bound workloads and a substantial reduction in resident memory thanks to switching from pre-forked processes to a single event loop.

---

## Target architecture

The Liteset server is designed along Clean Architecture lines. The application is split into four layers; dependencies point strictly inward — inner layers never import from outer ones.

| Layer | Responsibility | Implementation |
|---|---|---|
| **Presentation** | Controllers, DTOs, serialization, authorization predicates | `superset/controllers/`, `superset/schemas/`, `superset/guards/` — `async def` Litestar handlers, DTOs built on `msgspec.Struct`, Guards for RBAC |
| **Business Logic** | Business rules (`validate() → run()`) | `superset/commands/` — `AsyncBaseCommand`, framework-independent Command classes |
| **Data Access** | Data access through the SQLAlchemy 2.0 Select API | `superset/db/base_dao.py`, `superset/db/daos/` — `BaseAsyncDAO[T]` with an `AsyncSession` in the constructor |
| **Infrastructure** | Middleware, DI, configuration, DB engine | `superset/middleware/`, `superset/dependencies.py`, `superset/config.py` |

---

## Technology stack

| Category | Component | Role                                                                        |
|---|---|-----------------------------------------------------------------------------|
| **ASGI framework** | [Litestar](https://litestar.dev/) | Routing, DI, OpenAPI, Guards, Middleware                                    |
| **ASGI server** | Uvicorn + uvloop | libuv-based event loop                                                      |
| **ORM** | SQLAlchemy 2.0 (Async) | Declarative models, database queries                                        |
| **Metadata driver** | asyncpg / aiosqlite | Async access to the metadata DB                                             |
| **Serialization** | [msgspec](https://jcristharif.com/msgspec/) | DTOs + validation, replaces Marshmallow and Pydantic v1                     |
| **Configuration** | pydantic-settings | Typed configuration with backward compatibility for `superset_config.py`    |
| **Migrations** | Alembic (psycopg2, sync) | DB schema is inherited 1:1 from Superset 6.0.0                              |
| **Background jobs** | Celery | Left unchanged (orthogonal to the HTTP layer)                               |
| **WebSocket** | Native Litestar | Replaces the standalone Node.js `superset-websocket` service                |
| **Cache** | Redis (redis-py async) | Per-request cache, auth user cache, async events                            |
| **Logging** | structlog | Structured JSON logs                                                        |

---

## Compatibility guarantees

Liteset is a **drop-in replacement** for Apache Superset 6.0.0 at the backend level. Three invariants are locked in:

### 1. Metadata database

The schema of the metadata tables (`ab_user`, `ab_role`, `dashboards`, `slices`, `tables`, `dbs`, `query`, `saved_query`, `report_schedule`, etc.) is inherited without changes. Alembic revisions are carried over wholesale. An existing Superset installation can be migrated by simply swapping the backend — no `superset db upgrade` required.

### 2. Frontend

**The frontend code (`superset-frontend/`) must not be modified.** Liteset is obliged to reproduce every endpoint, JSON response shape, session cookie format (Flask-signed session cookies are decoded natively), CSRF tokens (`X-CSRFToken`), rison request parameters and the `/superset/welcome` SPA template.

### 3. HTTP API

All 37+ REST controllers reproduce the Superset contract 1:1 — URL routes, response codes, field names (dual `camelCase`/`snake_case` lookup is supported on the msgspec side), pagination shape, SIP-40 errors, Swagger spec layout. OpenAPI docs are auto-generated at `/swagger/v1`.

---

## Project layout

```
liteset/
├── superset/                       # Async backend on Litestar
│   ├── app.py                      # Litestar application factory
│   ├── config.py                   # SupersetSettings (pydantic-settings)
│   ├── dependencies.py             # DI Provide's (session, user, security_manager)
│   ├── exceptions.py               # SIP-40 hierarchy + handlers
│   ├── controllers/                # Presentation layer — 37 controllers
│   │   ├── base.py                 # RISON helpers, pagination, serialization
│   │   ├── chart.py, dashboard.py, database.py, dataset.py, …
│   │   └── sqllab.py, report.py, security.py, user.py, …
│   ├── commands/                   # Business Logic layer
│   │   ├── base.py                 # AsyncBaseCommand (validate/run)
│   │   └── chart.py, dashboard.py, database.py, …
│   ├── db/
│   │   ├── session.py              # AsyncEngine, async_sessionmaker
│   │   ├── base_dao.py             # BaseAsyncDAO[T]
│   │   ├── daos/                   # Data Access layer
│   │   │   ├── chart.py, dashboard.py, database.py, …
│   │   │   └── security.py, user.py, …
│   │   └── engine_specs/           # Async DB adapters
│   │       ├── base.py             # BaseAsyncEngineSpec
│   │       ├── postgres.py         # Native asyncpg
│   │       ├── mysql.py            # Native asyncmy
│   │       ├── clickhouse.py       # aiochclient
│   │       ├── trino.py            # aiotrino
│   │       └── sync_fallback.py    # Wrapper for other DBs via conn.run_sync()
│   ├── guards/                     # RBAC Guards
│   ├── middleware/                 # Auth, CSRF, locale, security headers, proxy fix
│   ├── schemas/                    # DTOs on msgspec.Struct
│   ├── security/                   # AsyncSecurityManager (FAB port)
│   ├── async_events/               # Redis-streams-based async events
│   ├── websocket/                  # Native Litestar WebSocket
│   ├── common/                     # QueryContext/QueryObject
│   ├── models/                     # SQLAlchemy 2.0 declarative models
│   ├── migrations/                 # Alembic (psycopg2, sync)
│   ├── db_engine_specs/            # Sync BaseEngineSpec (for SQL dialects)
│   ├── sql/                        # SQL parser, Jinja templating
│   ├── viz.py                      # Legacy viz engine (explore_json)
│   └── static/, templates/         # SPA bundle, Jinja templates
├── superset-frontend/              # React frontend (not modified)
├── tests/                          # pytest
├── requirements/                   # base.in, development.in, …
└── pyproject.toml
```

---

## Installation and running

See the Liteset [quickstart guide](https://liteset.happykust.dev/docs/quickstart/) or explore the [production deployment options](https://liteset.happykust.dev/docs/installation/architecture/).

---

## License

Liteset is distributed under the [Apache License 2.0](LICENSE.txt), inherited from Apache Superset. All files carried over from Apache Superset 6.0.0 preserve their original ASF headers.

---

<p>
  <em>Liteset is an academic port; the author may have missed important details that cause regressions relative to Apache Superset 6.0.0. For production installations, keep using <a href="https://github.com/apache/superset">apache/superset</a>.</em>
</p>
