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
"""Tests for the roles search API endpoint and AsyncRoleDAO."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from superset.schemas.security import RoleResponse, RolesSearchResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_role(
    role_id: int = 1,
    name: str = "Admin",
    user_ids: list[int] | None = None,
    permission_ids: list[int] | None = None,
) -> MagicMock:
    """Create a mock Role object mimicking FAB's Role model."""
    role = MagicMock()
    role.id = role_id
    role.name = name
    role.user = [MagicMock(id=uid) for uid in (user_ids or [])]
    role.permissions = [MagicMock(id=pid) for pid in (permission_ids or [])]
    return role


@pytest.fixture
def mock_admin_role() -> MagicMock:
    return _make_mock_role(
        role_id=1, name="Admin", user_ids=[1, 2], permission_ids=[10, 20]
    )


@pytest.fixture
def mock_gamma_role() -> MagicMock:
    return _make_mock_role(role_id=2, name="Gamma", user_ids=[3], permission_ids=[30])


@pytest.fixture
def mock_role_dao(mock_admin_role: MagicMock) -> AsyncMock:
    dao = AsyncMock()
    dao.search = AsyncMock(return_value=([mock_admin_role], 1))
    return dao


@pytest.fixture
def mock_role_dao_empty() -> AsyncMock:
    dao = AsyncMock()
    dao.search = AsyncMock(return_value=([], 0))
    return dao


@pytest.fixture
def mock_role_dao_multi(
    mock_admin_role: MagicMock, mock_gamma_role: MagicMock
) -> AsyncMock:
    dao = AsyncMock()
    dao.search = AsyncMock(return_value=([mock_admin_role, mock_gamma_role], 2))
    return dao


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestRoleSchemas:
    """Test msgspec schema serialization."""

    def test_role_response_defaults(self) -> None:
        role = RoleResponse(id=1, name="Admin")
        assert role.user_ids == []
        assert role.permission_ids == []

    def test_role_response_with_ids(self) -> None:
        role = RoleResponse(
            id=1, name="Admin", user_ids=[1, 2], permission_ids=[10, 20]
        )
        assert role.user_ids == [1, 2]
        assert role.permission_ids == [10, 20]

    def test_roles_search_response_to_builtins(self) -> None:
        resp = RolesSearchResponse(
            result=[RoleResponse(id=1, name="Admin")],
            count=1,
            ids=[1],
        )
        data = msgspec.to_builtins(resp)
        assert data["count"] == 1
        assert len(data["result"]) == 1
        assert data["result"][0]["name"] == "Admin"
        assert data["ids"] == [1]

    def test_roles_search_response_empty(self) -> None:
        resp = RolesSearchResponse()
        data = msgspec.to_builtins(resp)
        assert data["count"] == 0
        assert data["result"] == []
        assert data["ids"] == []


# ---------------------------------------------------------------------------
# Controller-level logic tests (unit, no HTTP transport)
# ---------------------------------------------------------------------------


class TestSearchRolesLogic:
    """Test the search_roles controller logic by calling the method directly."""

    @staticmethod
    def _call_search_roles() -> Any:
        """Return the unwrapped search_roles function, bypassing Litestar's handler
        wrapper.
        """
        from superset.controllers.security import SecurityController

        return SecurityController.search_roles.fn

    @pytest.mark.asyncio
    async def test_search_roles_returns_results(
        self, mock_role_dao: AsyncMock, mock_admin_role: MagicMock
    ) -> None:
        """Happy path: single role returned."""
        fn = self._call_search_roles()
        result = await fn(
            MagicMock(),  # self (controller instance)
            role_dao=mock_role_dao,
            rison_params=None,
        )

        assert result["count"] == 1
        assert len(result["result"]) == 1
        assert result["result"][0]["id"] == 1
        assert result["result"][0]["name"] == "Admin"
        assert result["result"][0]["user_ids"] == [1, 2]
        assert result["result"][0]["permission_ids"] == [10, 20]
        assert result["ids"] == [1]

        mock_role_dao.search.assert_awaited_once_with(
            name_filter=None,
            user_ids_filter=None,
            permission_ids_filter=None,
            group_ids_filter=None,
            order_column="id",
            order_direction="asc",
            page=0,
            page_size=10,
        )

    @pytest.mark.asyncio
    async def test_search_roles_empty(self, mock_role_dao_empty: AsyncMock) -> None:
        """No roles found returns empty response."""
        fn = self._call_search_roles()
        result = await fn(
            MagicMock(),
            role_dao=mock_role_dao_empty,
            rison_params=None,
        )

        assert result["count"] == 0
        assert result["result"] == []
        assert result["ids"] == []

    @pytest.mark.asyncio
    async def test_search_roles_with_name_filter(
        self, mock_role_dao: AsyncMock
    ) -> None:
        """Name filter is extracted from rison filters and passed to DAO."""
        fn = self._call_search_roles()
        rison = {
            "filters": [{"col": "name", "value": "Admin"}],
            "page": 0,
            "page_size": 10,
        }
        await fn(
            MagicMock(),
            role_dao=mock_role_dao,
            rison_params=rison,
        )

        mock_role_dao.search.assert_awaited_once_with(
            name_filter="Admin",
            user_ids_filter=None,
            permission_ids_filter=None,
            group_ids_filter=None,
            order_column="id",
            order_direction="asc",
            page=0,
            page_size=10,
        )

    @pytest.mark.asyncio
    async def test_search_roles_pagination(self, mock_role_dao: AsyncMock) -> None:
        """Page and page_size are forwarded to the DAO."""
        fn = self._call_search_roles()
        rison = {"page": 2, "page_size": 5}
        await fn(
            MagicMock(),
            role_dao=mock_role_dao,
            rison_params=rison,
        )

        mock_role_dao.search.assert_awaited_once_with(
            name_filter=None,
            user_ids_filter=None,
            permission_ids_filter=None,
            group_ids_filter=None,
            order_column="id",
            order_direction="asc",
            page=2,
            page_size=5,
        )

    @pytest.mark.asyncio
    async def test_search_roles_ordering(self, mock_role_dao: AsyncMock) -> None:
        """Order column and direction are forwarded."""
        fn = self._call_search_roles()
        rison = {"order_column": "name", "order_direction": "desc"}
        await fn(
            MagicMock(),
            role_dao=mock_role_dao,
            rison_params=rison,
        )

        mock_role_dao.search.assert_awaited_once_with(
            name_filter=None,
            user_ids_filter=None,
            permission_ids_filter=None,
            group_ids_filter=None,
            order_column="name",
            order_direction="desc",
            page=0,
            page_size=10,
        )

    @pytest.mark.asyncio
    async def test_search_roles_invalid_order_column_defaults_to_id(
        self, mock_role_dao: AsyncMock
    ) -> None:
        """Invalid order_column raises HTTP 400."""
        from litestar.exceptions import HTTPException

        fn = self._call_search_roles()
        rison = {"order_column": "evil_column"}
        with pytest.raises(HTTPException) as exc_info:
            await fn(
                MagicMock(),
                role_dao=mock_role_dao,
                rison_params=rison,
            )
        assert exc_info.value.status_code == 400
        mock_role_dao.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_roles_multiple_results(
        self, mock_role_dao_multi: AsyncMock
    ) -> None:
        """Multiple roles are serialized correctly."""
        fn = self._call_search_roles()
        result = await fn(
            MagicMock(),
            role_dao=mock_role_dao_multi,
            rison_params=None,
        )

        assert result["count"] == 2
        assert len(result["result"]) == 2
        assert result["ids"] == [1, 2]
        assert result["result"][0]["name"] == "Admin"
        assert result["result"][1]["name"] == "Gamma"

    @pytest.mark.asyncio
    async def test_search_roles_role_with_no_users_or_permissions(
        self,
    ) -> None:
        """Role with None user/permissions lists doesn't crash."""
        role = MagicMock()
        role.id = 5
        role.name = "Empty"
        role.user = None
        role.permissions = None
        dao = AsyncMock()
        dao.search = AsyncMock(return_value=([role], 1))

        fn = self._call_search_roles()
        result = await fn(
            MagicMock(),
            role_dao=dao,
            rison_params=None,
        )

        assert result["count"] == 1
        assert result["result"][0]["user_ids"] == []
        assert result["result"][0]["permission_ids"] == []


# ---------------------------------------------------------------------------
# RoleController.get_list — pagination default (original: page_size=10)
# ---------------------------------------------------------------------------


class TestRoleControllerGetList:
    """Test RoleController.get_list — focuses on page_size default of 10."""

    @staticmethod
    def _get_list_fn() -> Any:
        """Return the unwrapped get_list handler."""
        from superset.controllers.role import RoleController

        return RoleController.get_list.fn

    @pytest.mark.asyncio
    async def test_get_list_default_page_size_is_10(self) -> None:
        """When no page_size is given, the DAO must receive page_size=10
        (original security/api.py:298 default), not 25 (generic default).
        """
        role = MagicMock()
        role.id = 1
        role.name = "Admin"
        role.user = []
        role.permissions = []
        # groups attribute may not exist on the mock
        role.groups = []
        dao = AsyncMock()
        dao.search = AsyncMock(return_value=([role], 1))

        fn = self._get_list_fn()
        await fn(
            MagicMock(),  # self (controller instance)
            role_dao=dao,
            rison_params=None,
        )

        dao.search.assert_awaited_once_with(
            name_filter=None,
            user_ids_filter=None,
            permission_ids_filter=None,
            group_ids_filter=None,
            order_column="id",
            order_direction="asc",
            page=0,
            page_size=10,  # must be 10, not 25
        )

    @pytest.mark.asyncio
    async def test_get_list_explicit_page_size_respected(self) -> None:
        """Caller-supplied page_size overrides the default."""
        role = MagicMock()
        role.id = 2
        role.name = "Gamma"
        role.user = []
        role.permissions = []
        role.groups = []
        dao = AsyncMock()
        dao.search = AsyncMock(return_value=([role], 1))

        fn = self._get_list_fn()
        await fn(
            MagicMock(),
            role_dao=dao,
            rison_params={"page": 1, "page_size": 50},
        )

        dao.search.assert_awaited_once_with(
            name_filter=None,
            user_ids_filter=None,
            permission_ids_filter=None,
            group_ids_filter=None,
            order_column="id",
            order_direction="asc",
            page=1,
            page_size=50,
        )

    @pytest.mark.asyncio
    async def test_get_list_invalid_order_column_raises_400(self) -> None:
        """Invalid order_column raises HTTP 400."""
        from litestar.exceptions import HTTPException

        dao = AsyncMock()
        fn = self._get_list_fn()
        with pytest.raises(HTTPException) as exc_info:
            await fn(
                MagicMock(),
                role_dao=dao,
                rison_params={"order_column": "evil"},
            )
        assert exc_info.value.status_code == 400
        dao.search.assert_not_awaited()
