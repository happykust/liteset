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
"""Regression tests for ``GET /api/v1/tag/get_objects/`` access scoping.

R13-05: the original ``TagDAO.get_tagged_objects_by_tag_ids`` loads entities
through ``DashboardDAO/ChartDAO/SavedQueryDAO.find_by_ids`` whose
``base_filter`` (DashboardAccessFilter / ChartFilter / SavedQueryFilter)
scopes visibility — non-privileged users only see dashboards/charts they can
access and ONLY their own saved queries. The liteset port loaded entities with
a bare ``select(model).where(id.in_(ids))``, disclosing names/urls/owners of
inaccessible objects to anyone with ``can_read Tag``.

R13-06: the SavedQuery branch calls ``sq.creator()`` which reads the lazy
``created_by`` relationship — without ``selectinload(created_by)`` it raises
``MissingGreenlet`` (HTTP 500) as soon as a tagged saved query has
``created_by_fk`` set (i.e. always, for user-created saved queries).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _StubSecurityManager:
    """Non-admin user with no datasource/database/schema grants."""

    def is_admin(self, user: object) -> bool:
        return False

    def is_guest_user(self, user: object) -> bool:
        return False

    async def can_access_all_datasources(self, user: object = None) -> bool:
        return False

    async def get_accessible_database_ids(self, user: object = None) -> list[int]:
        return []

    async def user_view_menu_names(
        self, permission_name: str, user: object = None
    ) -> set[str]:
        return set()

    async def get_user_roles(self, user: object = None) -> list[object]:
        return []


@pytest.fixture
async def tag_env():
    """Real models on a pure async engine: two users, two dashboards
    (one owned per user), one chart, two saved queries (one per user) —
    all linked to a single tag."""
    import superset.models  # noqa: F401  (register models)
    from superset.db.daos.tag import AsyncTagDAO
    from superset.models.dashboard import Dashboard
    from superset.models.helpers import Base
    from superset.models.security import User
    from superset.models.slice import Slice
    from superset.models.sql_lab import SavedQuery
    from superset.models.tags import Tag, TaggedObject

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as seed:
        victim = User(
            username="victim",
            first_name="vic",
            last_name="tim",
            email="victim@test.com",
            active=True,
            roles=[],
        )
        other = User(
            username="other",
            first_name="ot",
            last_name="her",
            email="other@test.com",
            active=True,
            roles=[],
        )
        seed.add_all([victim, other])
        await seed.flush()

        own_dash = Dashboard(
            dashboard_title="my dash",
            published=False,
            owners=[victim],
            slices=[],
            roles=[],
        )
        foreign_dash = Dashboard(
            dashboard_title="their dash",
            published=False,
            owners=[other],
            slices=[],
            roles=[],
        )
        chart = Slice(
            slice_name="a chart",
            datasource_id=1,
            datasource_type="table",
            params="{}",
            owners=[other],
            tags=[],
        )
        own_sq = SavedQuery(
            label="my query",
            sql="SELECT 1",
            created_by_fk=victim.id,
            tags=[],
        )
        foreign_sq = SavedQuery(
            label="their query",
            sql="SELECT 2",
            created_by_fk=other.id,
            tags=[],
        )
        seed.add_all([own_dash, foreign_dash, chart, own_sq, foreign_sq])
        await seed.flush()

        tag = Tag(name="shared-tag", type="custom")
        seed.add(tag)
        await seed.flush()
        seed.add_all(
            [
                TaggedObject(
                    tag_id=tag.id, object_id=own_dash.id, object_type="dashboard"
                ),
                TaggedObject(
                    tag_id=tag.id, object_id=foreign_dash.id, object_type="dashboard"
                ),
                TaggedObject(tag_id=tag.id, object_id=chart.id, object_type="chart"),
                TaggedObject(tag_id=tag.id, object_id=own_sq.id, object_type="query"),
                TaggedObject(
                    tag_id=tag.id, object_id=foreign_sq.id, object_type="query"
                ),
            ]
        )
        await seed.commit()
        tag_id = tag.id
        victim_id = victim.id

    # Fresh session so nothing is cached in the identity map — lazy loads
    # would really hit the DB (and raise MissingGreenlet if unprotected).
    async with session_factory() as session:
        yield AsyncTagDAO(session), tag_id, victim_id, session_factory
    await engine.dispose()


async def test_get_objects_scoped_excludes_inaccessible(tag_env) -> None:
    """R13-05: with security_manager+user the result must match upstream's
    base_filter scoping — own dashboard and own saved query only; the foreign
    dashboard, the chart (no datasource grants) and the foreign saved query
    must be invisible."""
    dao, tag_id, victim_id, session_factory = tag_env

    class _User:
        id = victim_id

    results = await dao.get_tagged_objects_by_tag_ids(
        [tag_id],
        security_manager=_StubSecurityManager(),
        user=_User(),
    )
    by_name = {(r["type"], r["name"]) for r in results}
    assert ("dashboard", "my dash") in by_name
    assert ("query", "my query") in by_name
    assert ("dashboard", "their dash") not in by_name
    assert ("chart", "a chart") not in by_name
    assert ("query", "their query") not in by_name


async def test_get_objects_unscoped_no_missing_greenlet(tag_env) -> None:
    """R13-06: the SavedQuery branch serialises ``sq.creator()`` which reads
    the lazy ``created_by`` relationship.  On a fresh async session this must
    NOT raise MissingGreenlet — and the creator name must be populated."""
    dao, tag_id, _victim_id, _session_factory = tag_env

    results = await dao.get_tagged_objects_by_tag_ids([tag_id])
    queries = {r["name"]: r for r in results if r["type"] == "query"}
    assert set(queries) == {"my query", "their query"}
    # creator() resolved through the preloaded relationship, not lazy IO
    assert queries["my query"]["creator"] != ""


async def test_controller_get_objects_passes_scope() -> None:
    """The controller must forward security_manager + current_user so the DAO
    applies the access filters (without them the load is unscoped)."""
    from superset.controllers.tag import TagController

    handler = TagController.get_objects
    fn = handler.fn if hasattr(handler, "fn") else handler

    request = MagicMock()
    request.query_params = {"tagIds": "1,2", "tags": "", "types": ""}
    dao = MagicMock()
    dao.get_tagged_objects_by_tag_ids = AsyncMock(return_value=[])
    sm = MagicMock()
    user = MagicMock()

    controller = TagController(owner=MagicMock())
    await fn(
        controller,
        request=request,
        dao=dao,
        current_user=user,
        security_manager=sm,
    )
    dao.get_tagged_objects_by_tag_ids.assert_awaited_once_with(
        [1, 2],
        obj_types=None,
        security_manager=sm,
        user=user,
    )
