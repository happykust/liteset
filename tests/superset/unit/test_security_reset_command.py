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
"""Tests for ResetSupersetCommand (factory reset) on production models.

Regression guard for the ``superset reset`` CLI chain: the command reads
``security_manager.user_model`` / ``role_model`` (the FAB contract) and
the CLI builds ``AsyncSecurityManager(AsyncSecurityDAO(session))`` — both
once referenced phantom attributes and crashed unconditionally.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from superset.commands.security.reset import ResetSupersetCommand
from superset.exceptions import CommandInvalidError, ForbiddenError
from superset.security.dao import AsyncSecurityDAO
from superset.security.manager import AsyncSecurityManager


@pytest.fixture
async def reset_env():
    """In-memory DB with the full production schema + a minimal RBAC seed."""
    import superset.models  # noqa: F401  (register every model on Base.metadata)
    from superset.models.helpers import Base
    from superset.models.security import Role, User

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        admin_role = Role(name="Admin")
        custom_role = Role(name="CustomRole")
        admin = User(
            username="admin",
            first_name="ad",
            last_name="min",
            email="admin@test.com",
            active=True,
        )
        admin.roles.append(admin_role)
        bob = User(
            username="bob",
            first_name="b",
            last_name="ob",
            email="bob@test.com",
            active=True,
        )
        bob.roles.append(custom_role)
        session.add_all([admin_role, custom_role, admin, bob])
        await session.commit()
        yield session, admin
    await engine.dispose()


def _make_command(session, user, **kwargs):
    manager = AsyncSecurityManager(AsyncSecurityDAO(session))
    return ResetSupersetCommand(
        session=session,
        security_manager=manager,
        confirm=kwargs.pop("confirm", True),
        user=user,
        **kwargs,
    )


async def test_reset_requires_confirmation(reset_env):
    session, admin = reset_env
    cmd = _make_command(session, admin, confirm=False)
    with pytest.raises(CommandInvalidError, match="Reset aborted"):
        await cmd.validate()


async def test_reset_requires_active_user(reset_env):
    session, _ = reset_env
    cmd = _make_command(session, None)
    with pytest.raises(ForbiddenError, match="User not found"):
        await cmd.validate()


async def test_reset_wipes_non_system_users_and_roles(reset_env):
    session, admin = reset_env
    from superset.models.core import Log
    from superset.models.security import Role, User

    cmd = _make_command(session, admin)
    await cmd.execute()

    usernames = set(
        (await session.execute(select(User.username))).scalars().all()
    )
    role_names = set((await session.execute(select(Role.name))).scalars().all())
    assert usernames == {"admin"}
    assert "CustomRole" not in role_names
    assert "Admin" in role_names

    log_actions = (await session.execute(select(Log.action))).scalars().all()
    assert "Factory Reset" in log_actions


async def test_reset_honors_exclusions(reset_env):
    session, admin = reset_env
    from superset.models.security import Role, User

    cmd = _make_command(
        session, admin, exclude_users="bob", exclude_roles="CustomRole"
    )
    await cmd.execute()

    usernames = set(
        (await session.execute(select(User.username))).scalars().all()
    )
    role_names = set((await session.execute(select(Role.name))).scalars().all())
    assert "bob" in usernames
    assert "CustomRole" in role_names
