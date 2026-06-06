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
"""Unit tests for ``query_access_filters`` (SQL Lab query RBAC scoping).

Mirrors ``superset_old/queries/filters.py::QueryFilter``: a user holding the
``all_query_access`` permission (gated through
``security_manager.can_access_all_queries``) sees every query; everyone else is
scoped to the queries they own (``Query.user_id == <id>``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.db.filters import query_access_filters
from superset.models.sql_lab import Query


def _make_security_manager(*, is_admin: bool, can_access_all: bool) -> MagicMock:
    sm = MagicMock()
    sm.is_admin = MagicMock(return_value=is_admin)
    sm.can_access_all_queries = AsyncMock(return_value=can_access_all)
    return sm


@pytest.mark.asyncio
async def test_query_access_filters_all_query_access_no_restriction():
    """User with ``all_query_access`` gets the all-access branch (no filter)."""
    sm = _make_security_manager(is_admin=False, can_access_all=True)
    user = MagicMock()
    user.id = 42

    filters = await query_access_filters(sm, user)

    assert filters == []
    sm.can_access_all_queries.assert_awaited_once_with(user=user)


@pytest.mark.asyncio
async def test_query_access_filters_without_permission_scoped_to_owner():
    """User without ``all_query_access`` is scoped to ``created_by`` (user_id)."""
    sm = _make_security_manager(is_admin=False, can_access_all=False)
    user = MagicMock()
    user.id = 42

    filters = await query_access_filters(sm, user)

    assert len(filters) == 1
    # The scoped branch must constrain on the owning user's id.
    expected = Query.user_id == 42
    assert str(filters[0]) == str(expected)
    sm.can_access_all_queries.assert_awaited_once_with(user=user)


@pytest.mark.asyncio
async def test_query_access_filters_admin_bypass():
    """Admins short-circuit to the all-access branch before the permission check."""
    sm = _make_security_manager(is_admin=True, can_access_all=False)
    user = MagicMock()
    user.id = 1

    filters = await query_access_filters(sm, user)

    assert filters == []
    sm.can_access_all_queries.assert_not_awaited()
