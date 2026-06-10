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
"""Regression: dashboard objects used by security checks must not lazy-load.

The dashboard-RBAC fallback in ``raise_for_access`` (and ``can_access_dashboard``)
reads ``roles``/``owners``/``slices`` SYNCHRONOUSLY. ``_get_dashboard_by_id``
used to be a bare ``select(Dashboard)`` — any of those reads tripped a sync
lazy-load on the async session → ``MissingGreenlet`` → HTTP 500 for every
non-admin RBAC/embedded fallback. Path 4 (``raise_for_access(dashboard=...)``)
likewise called ``is_owner`` on a bare-fetched dashboard (the welcome-page
lookup in spa.py) with the crash swallowed into a silent 404.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def dash_env():
    import superset.models  # noqa: F401  (register models)
    from superset.models.dashboard import Dashboard
    from superset.models.helpers import Base
    from superset.models.security import Role, User

    # PURE async engine (no sync ``creator=`` shortcut): a sync raw
    # connection would bypass the greenlet guard and silently allow lazy
    # loads, making these tests unable to reproduce MissingGreenlet.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as seed:
        role = Role(name="RBACViewers")
        owner = User(
            username="owner",
            first_name="ow",
            last_name="ner",
            email="owner@test.com",
            active=True,
            roles=[],  # pre-init: sync .roles read must not lazy-load
        )
        viewer = User(
            username="viewer",
            first_name="vi",
            last_name="ewer",
            email="viewer@test.com",
            active=True,
            roles=[role],
        )
        dash = Dashboard(
            dashboard_title="rbac dash",
            slug="rbac-dash",
            published=True,
            roles=[role],
            owners=[owner],
            slices=[],
        )
        seed.add_all([role, owner, viewer, dash])
        await seed.commit()
        yield session_factory, dash.id, owner, viewer
    await engine.dispose()


def _make_manager(session, **kwargs):
    from superset.security.dao import AsyncSecurityDAO
    from superset.security.manager import AsyncSecurityManager

    return AsyncSecurityManager(AsyncSecurityDAO(session), **kwargs)


async def test_get_dashboard_by_id_eager_loads_sync_read_attrs(dash_env):
    """The fallback loader must pre-load roles/owners/slices (no lazy-load)."""
    session_factory, dash_id, _owner, _viewer = dash_env
    async with session_factory() as fresh:  # fresh identity map — nothing cached
        manager = _make_manager(fresh, dashboard_rbac_enabled=True)
        dash = await manager._get_dashboard_by_id(dash_id)
        assert dash is not None
        # Loaded relationships live in __dict__; a bare select leaves them out
        # and the sync getattr below would raise MissingGreenlet.
        for attr in ("roles", "owners", "slices"):
            assert attr in dash.__dict__, f"{attr} not eager-loaded"
        assert [r.name for r in dash.roles] == ["RBACViewers"]
        assert [o.username for o in dash.owners] == ["owner"]
        assert dash.slices == []


async def test_can_access_dashboard_rbac_on_fallback_loaded_dashboard(dash_env):
    """RBAC role-match grants access on the fallback-loaded dashboard."""
    session_factory, dash_id, _owner, viewer = dash_env
    async with session_factory() as fresh:
        manager = _make_manager(fresh, dashboard_rbac_enabled=True)
        dash = await manager._get_dashboard_by_id(dash_id)
        assert await manager.can_access_dashboard(dash, user=viewer) is True


async def test_raise_for_access_path4_bare_dashboard_owner_passes(dash_env):
    """Path 4 must tolerate a bare-fetched dashboard (welcome-page lookup)."""
    from superset.models.dashboard import Dashboard

    session_factory, dash_id, owner, _viewer = dash_env
    async with session_factory() as fresh:
        manager = _make_manager(fresh)
        bare = (
            await fresh.execute(select(Dashboard).where(Dashboard.id == dash_id))
        ).scalars().one()
        assert "owners" not in bare.__dict__  # genuinely bare
        # Before the fix: MissingGreenlet out of is_owner. Now: owner passes.
        await manager.raise_for_access(dashboard=bare, user=owner)


async def test_raise_for_access_path4_bare_dashboard_denied_non_owner(dash_env):
    """Same bare-fetch path still DENIES a non-owner without RBAC match."""
    from superset.exceptions import SupersetSecurityException
    from superset.models.dashboard import Dashboard
    from superset.models.security import User

    session_factory, dash_id, _owner, _viewer = dash_env
    async with session_factory() as fresh:
        stranger = User(
            username="stranger",
            first_name="st",
            last_name="ranger",
            email="stranger@test.com",
            active=True,
            roles=[],
        )
        manager = _make_manager(fresh, dashboard_rbac_enabled=True)
        bare = (
            await fresh.execute(select(Dashboard).where(Dashboard.id == dash_id))
        ).scalars().one()
        with pytest.raises(SupersetSecurityException):
            await manager.raise_for_access(dashboard=bare, user=stranger)
