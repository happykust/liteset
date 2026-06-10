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
"""Tests for superset/db/engine.py — create_db_engine isolation_level parity.

The original SupersetAppInitializer.set_db_default_isolation called::

    db.engine.execution_options(isolation_level=set_isolation_level_to)

without storing the returned OptionEngine.  SQLAlchemy's execution_options()
returns a **new** engine and leaves the original unmodified, making the call
a silent no-op.  Therefore the original Flask Superset always ran with each
database driver's own default isolation level (REPEATABLE READ for MySQL,
READ COMMITTED for PostgreSQL).

create_db_engine must replicate that no-op: it must NOT auto-inject
isolation_level for any dialect.  Only an explicitly passed isolation_level
kwarg should reach the underlying create_async_engine.
"""

from __future__ import annotations

import superset.db.engine as eng_mod


def test_mysql_url_no_auto_isolation_level(monkeypatch: object) -> None:
    """MySQL URL: isolation_level must NOT be injected automatically.

    Original behaviour: MySQL ran with REPEATABLE READ (MySQL default) because
    set_db_default_isolation was a no-op.
    """
    captured: dict = {}

    def fake_create(url: str, **kw: object) -> str:
        captured["url"] = url
        captured["kw"] = kw
        return "ENGINE"

    monkeypatch.setattr(eng_mod, "_create_async_engine", fake_create)
    monkeypatch.setattr(eng_mod, "_engine", None)

    eng_mod.create_db_engine("mysql+aiomysql://user:pass@localhost/db")

    assert "isolation_level" not in captured["kw"], (
        "create_db_engine must not auto-inject isolation_level for MySQL; "
        "original set_db_default_isolation was a no-op"
    )


def test_postgresql_url_no_auto_isolation_level(monkeypatch: object) -> None:
    """PostgreSQL URL: isolation_level must NOT be injected automatically.

    Original behaviour: set_db_default_isolation was also a no-op for PG
    (PG defaults to READ COMMITTED anyway, so there is no observable
    difference, but the principle holds — no injection should occur).
    """
    captured: dict = {}

    def fake_create(url: str, **kw: object) -> str:
        captured["kw"] = kw
        return "ENGINE"

    monkeypatch.setattr(eng_mod, "_create_async_engine", fake_create)
    monkeypatch.setattr(eng_mod, "_engine", None)

    eng_mod.create_db_engine("postgresql+asyncpg://user:pass@localhost/db")

    assert "isolation_level" not in captured["kw"], (
        "create_db_engine must not auto-inject isolation_level for PostgreSQL"
    )


def test_explicit_isolation_level_is_passed_through(monkeypatch: object) -> None:
    """Caller-supplied isolation_level must reach create_async_engine unchanged."""
    captured: dict = {}

    def fake_create(url: str, **kw: object) -> str:
        captured["kw"] = kw
        return "ENGINE"

    monkeypatch.setattr(eng_mod, "_create_async_engine", fake_create)
    monkeypatch.setattr(eng_mod, "_engine", None)

    eng_mod.create_db_engine(
        "mysql+aiomysql://user:pass@localhost/db",
        isolation_level="SERIALIZABLE",
    )

    assert captured["kw"]["isolation_level"] == "SERIALIZABLE"


def test_sqlite_url_no_isolation_level(monkeypatch: object) -> None:
    """SQLite URL: no isolation_level injected (unchanged from before fix)."""
    captured: dict = {}

    def fake_create(url: str, **kw: object) -> str:
        captured["kw"] = kw
        return "ENGINE"

    monkeypatch.setattr(eng_mod, "_create_async_engine", fake_create)
    monkeypatch.setattr(eng_mod, "_engine", None)

    eng_mod.create_db_engine("sqlite+aiosqlite:///test.db")

    assert "isolation_level" not in captured["kw"]


def test_get_engine_returns_created_engine(monkeypatch: object) -> None:
    """get_engine() returns the engine set by create_db_engine."""
    monkeypatch.setattr(eng_mod, "_create_async_engine", lambda url, **kw: "SENTINEL")
    monkeypatch.setattr(eng_mod, "_engine", None)

    eng_mod.create_db_engine("sqlite+aiosqlite://")

    assert eng_mod.get_engine() == "SENTINEL"
