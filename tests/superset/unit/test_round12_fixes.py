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
"""Round-12 regressions (manual full-pass findings).

* R12-02 (HIGH) — ``dashboard/importers/v1/utils.py::_import_dashboard`` called
  ``can_access`` / ``can_access_dashboard`` without the required keyword-only
  ``user=`` and ``await``-ed the synchronous ``is_admin()`` without its ``user``
  argument — TypeError 500 on every API dashboard / asset bundle import that
  carries a security manager.  Same bug class R10/R11 fixed in the chart
  importer; the dashboard copy was missed.
* R12-01 (low) — ``theme_import.py`` called ``can_access`` without ``user=``
  (latent — only fires when ``ignore_permissions=False``).
* R12-03 (low) — ``tag.py`` GET ``/{pk}/favorites/`` lacked the
  ``can_read Tag`` guard its sibling endpoints carry.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# R12-02 — dashboard importer security calls use the correct signatures
# ---------------------------------------------------------------------------


@pytest.fixture
async def async_session():
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import superset.models  # noqa: F401  (register models)
    from superset.models.helpers import Base

    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


def test_import_dashboard_source_passes_user_to_security_calls():
    """Static guard: the three security calls must thread the user through."""
    from superset.commands.dashboard.importers.v1 import utils as dash_utils

    src = inspect.getsource(dash_utils._import_dashboard)
    # can_access must be user-explicit
    assert 'can_access(\n            "can_write", "Dashboard", user=current_user' in src
    # can_access_dashboard must pass user=
    assert (
        "can_access_dashboard(\n                    existing, user=current_user" in src
    )
    # is_admin must be SYNC (no await) and take the user positionally
    assert "is_admin = security_manager.is_admin(current_user)" in src
    assert "await security_manager.is_admin()" not in src


@pytest.mark.asyncio
async def test_import_dashboard_overwrite_denied_does_not_raise_typeerror(
    async_session: Any,
) -> None:
    """The overwrite-permission branch must run real security checks (no
    TypeError) and deny a non-owner non-admin with ImportFailedError."""
    from superset.commands.dashboard.importers.v1.utils import _import_dashboard
    from superset.exceptions import ImportFailedError
    from superset.models.dashboard import Dashboard

    existing = Dashboard(dashboard_title="r12", slug="r12-slug")
    existing.uuid = __import__("uuid").UUID("33333333-0000-0000-0000-000000000012")
    existing.owners = []
    async_session.add(existing)
    await async_session.flush()

    sm = MagicMock()
    sm.can_access = AsyncMock(return_value=True)  # can_write Dashboard
    sm.can_access_dashboard = AsyncMock(return_value=False)  # no access to it
    sm.is_admin = MagicMock(return_value=False)  # sync
    user = MagicMock()
    user.id = 7

    config = {
        "uuid": "33333333-0000-0000-0000-000000000012",
        "dashboard_title": "r12",
        "slug": "r12-slug",
    }
    with pytest.raises(ImportFailedError):
        await _import_dashboard(
            async_session,
            dict(config),
            overwrite=True,
            security_manager=sm,
            current_user=user,
        )
    # The fix means is_admin was called SYNC with the user, and
    # can_access_dashboard with user= — assert they were invoked correctly.
    sm.is_admin.assert_called_once_with(user)
    sm.can_access_dashboard.assert_awaited_once()
    _, kwargs = sm.can_access_dashboard.call_args
    assert kwargs.get("user") is user


# ---------------------------------------------------------------------------
# R12-01 — theme_import threads the user into can_access
# ---------------------------------------------------------------------------


def test_theme_import_can_access_passes_user():
    from superset.commands import theme_import

    src = inspect.getsource(theme_import.import_theme)
    assert 'can_access(\n            "can_write", "Theme", user=_user' in src
    assert 'can_access("can_write", "Theme")' not in src


# ---------------------------------------------------------------------------
# R12-03 — tag check_favorite is guarded by can_read Tag
# ---------------------------------------------------------------------------


def test_tag_check_favorite_has_can_read_guard():
    from superset.controllers.tag import TagController

    handler = TagController.check_favorite
    guards = getattr(handler, "guards", None) or []
    # Litestar stores guard callables; our require_permission returns a closure
    # whose freevars include the (action, resource) tuple.
    found = False
    for g in guards:
        cells = getattr(g, "__closure__", None) or []
        vals = [c.cell_contents for c in cells]
        if ("can_read", "Tag") in vals or "can_read" in vals:
            found = True
    assert found, "check_favorite must be guarded by require_permission(can_read, Tag)"
