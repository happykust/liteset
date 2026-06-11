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
"""Round-10 security/sqllab regressions.

* SQL Lab async dispatch: the PENDING Query row must be COMMITTED before
  ``get_sql_results.delay()`` — the Celery worker reads the row through its
  own sync session under READ COMMITTED and cannot see an uncommitted row
  (upstream ``_save_new_query`` commits explicitly for exactly this reason).
* ``AsyncSecurityManager.auth_user_oauth`` — was a phantom symbol
  (``OAuthAuthBackend.handle_callback`` called it but the method never
  existed) → AttributeError on every OAuth login.  Now a 1:1 FAB port.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# auth_user_oauth (1:1 FAB port — was a phantom symbol)
# ---------------------------------------------------------------------------


def _make_sm(user=None, role=None):
    from superset.security.manager import AsyncSecurityManager

    dao = MagicMock()
    dao.get_user_by_username = AsyncMock(return_value=user)
    dao.get_role_by_name = AsyncMock(return_value=role)
    sm = AsyncSecurityManager(dao)
    return sm, dao


@pytest.mark.asyncio
async def test_auth_user_oauth_existing_user_logs_in():
    user = SimpleNamespace(id=7, username="alice", active=True, roles=[])
    sm, _dao = _make_sm(user=user)
    sm._update_user_auth_stat = AsyncMock()

    settings = SimpleNamespace(
        auth_user_registration=False,
        auth_roles_sync_at_login=False,
        auth_roles_mapping={},
    )
    result = await sm.auth_user_oauth({"username": "alice"}, settings=settings)

    assert result is user
    sm._update_user_auth_stat.assert_awaited_once_with(user, success=True)


@pytest.mark.asyncio
async def test_auth_user_oauth_unknown_user_no_registration():
    sm, _dao = _make_sm(user=None)
    settings = SimpleNamespace(
        auth_user_registration=False,
        auth_roles_sync_at_login=False,
        auth_roles_mapping={},
    )
    result = await sm.auth_user_oauth({"username": "ghost"}, settings=settings)
    assert result is None


@pytest.mark.asyncio
async def test_auth_user_oauth_self_registration():
    role = SimpleNamespace(id=3, name="Gamma")
    sm, _dao = _make_sm(user=None, role=role)
    registered = SimpleNamespace(id=11, username="bob", active=True)
    sm._register_user = AsyncMock(return_value=registered)
    sm._update_user_auth_stat = AsyncMock()

    settings = SimpleNamespace(
        auth_user_registration=True,
        auth_user_registration_role="Gamma",
        auth_user_registration_role_jmespath=None,
        auth_roles_sync_at_login=False,
        auth_roles_mapping={},
    )
    result = await sm.auth_user_oauth(
        {"username": "bob", "email": "bob@x.com", "first_name": "B"},
        settings=settings,
    )

    assert result is registered
    kwargs = sm._register_user.await_args.kwargs
    assert kwargs["username"] == "bob"
    assert kwargs["email"] == "bob@x.com"
    assert kwargs["roles"] == [role]


@pytest.mark.asyncio
async def test_auth_user_oauth_inactive_user_denied():
    user = SimpleNamespace(id=7, username="alice", active=False, roles=[])
    sm, _dao = _make_sm(user=user)
    settings = SimpleNamespace(
        auth_user_registration=True,
        auth_roles_sync_at_login=False,
        auth_roles_mapping={},
    )
    assert (await sm.auth_user_oauth({"username": "alice"}, settings=settings)) is None


@pytest.mark.asyncio
async def test_auth_user_oauth_no_username_or_email():
    sm, _dao = _make_sm()
    settings = SimpleNamespace(auth_user_registration=True)
    assert (await sm.auth_user_oauth({"sub": "x"}, settings=settings)) is None


@pytest.mark.asyncio
async def test_async_dispatch_commits_pending_row_before_delay(monkeypatch):
    from superset.commands.sqllab.execute import ExecuteSQLCommand

    events: list[str] = []

    session = MagicMock()
    session.add = MagicMock()

    async def _flush() -> None:
        events.append("flush")

    async def _commit() -> None:
        events.append("commit")

    session.flush = _flush
    session.commit = _commit
    # ``query.database = db_row`` goes through the ORM relationship, which
    # requires a mapped instance — use a real (detached) Database.
    from superset.models.core import Database

    db_row = Database(database_name="testdb", sqlalchemy_uri="sqlite://")
    session.get = AsyncMock(return_value=db_row)

    dao = MagicMock()
    dao.session = session
    dao.find_one_or_none = AsyncMock(return_value=None)
    dao.save_metadata = AsyncMock()

    cmd = ExecuteSQLCommand(
        dao=dao,
        database_id=1,
        sql="SELECT 1",
        run_async=True,
        user_id=1,
    )
    cmd._run_async = True
    monkeypatch.setattr(
        ExecuteSQLCommand, "_render_jinja", AsyncMock(return_value="SELECT 1")
    )

    import superset.tasks.sql_lab as sql_lab_tasks

    class _Task:
        def forget(self) -> None:
            pass

    def _delay(**kwargs):
        events.append("delay")
        return _Task()

    monkeypatch.setattr(sql_lab_tasks.get_sql_results, "delay", _delay)

    result = await cmd.run()

    assert "query" in result
    assert "delay" in events, "Celery dispatch did not happen"
    assert "commit" in events, (
        "PENDING Query row was never committed before Celery dispatch — the "
        "worker (separate process, READ COMMITTED) cannot see the row."
    )
    assert events.index("commit") < events.index("delay")
