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
"""Tests for the Celery-worker async engine factory.

``create_worker_engine`` must use NullPool (Celery tasks run async work via
``asyncio.run()`` — a fresh event loop per task — and pooled asyncpg
connections cannot cross loops) and must set the module-global engine so
``get_engine()`` works inside tasks.
"""

from __future__ import annotations

from sqlalchemy.pool import NullPool

import superset.db.session as sess


def test_worker_engine_uses_nullpool_sets_global_and_pg_isolation(monkeypatch):
    captured: dict = {}

    def fake_create(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return "ENGINE_SENTINEL"

    monkeypatch.setattr(sess, "_create_async_engine", fake_create)
    monkeypatch.setattr(sess, "_engine", None)

    eng = sess.create_worker_engine("postgresql+asyncpg://u:p@h:5432/db")

    assert eng == "ENGINE_SENTINEL"
    assert sess.get_engine() == "ENGINE_SENTINEL"  # module global was set
    assert captured["kw"]["poolclass"] is NullPool
    # READ COMMITTED applied for PostgreSQL, exactly like create_db_engine.
    assert captured["kw"]["isolation_level"] == "READ COMMITTED"


def test_worker_engine_sqlite_has_no_isolation_level(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(
        sess, "_create_async_engine", lambda url, **kw: captured.update(kw) or "E"
    )
    monkeypatch.setattr(sess, "_engine", None)

    sess.create_worker_engine("sqlite+aiosqlite:///x.db")

    assert captured["poolclass"] is NullPool
    assert "isolation_level" not in captured  # sqlite: no READ COMMITTED


def test_worker_engine_respects_explicit_isolation(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(
        sess, "_create_async_engine", lambda url, **kw: captured.update(kw) or "E"
    )
    monkeypatch.setattr(sess, "_engine", None)

    sess.create_worker_engine(
        "postgresql+asyncpg://u:p@h/db", isolation_level="SERIALIZABLE"
    )

    assert captured["isolation_level"] == "SERIALIZABLE"  # caller override wins
    assert captured["poolclass"] is NullPool
