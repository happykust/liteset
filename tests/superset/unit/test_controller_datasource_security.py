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
"""Unit tests for DatasourceController._call_raise_for_access dispatch.

Regression: for datasource_type='query' the original calls
  security_manager.raise_for_access(query=self)  (Path 1 — DB+table check)
whereas liteset previously called
  security_manager.raise_for_access(datasource=self)  (Path 3 — datasource check).
Path 3 evaluates Query.perm as a datasource_access string (never registered in FAB),
denying users who only have table-level permissions.

These tests verify that _call_raise_for_access routes correctly, and that
get_column_values / get_datasource delegate to it (not hardcode datasource=).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.controllers.datasource import DatasourceController
from superset.utils.core import DatasourceType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_security_manager() -> MagicMock:
    sm = MagicMock()
    sm.raise_for_access = AsyncMock(return_value=None)
    return sm


def _make_user() -> MagicMock:
    user = MagicMock()
    user.id = 42
    return user


def _make_datasource(ds_type: str = "table") -> MagicMock:
    ds = MagicMock()
    ds.id = 1
    ds.type = ds_type
    ds.perm = "[db].[tab](id:1)"
    return ds


# ---------------------------------------------------------------------------
# _call_raise_for_access dispatch
# ---------------------------------------------------------------------------


class TestCallRaiseForAccessDispatch:
    """_call_raise_for_access routes the kwargs correctly per datasource type."""

    @pytest.mark.anyio
    async def test_query_type_passes_query_kwarg(self) -> None:
        """datasource_type='query' must route through Path 1 (query= kwarg)."""
        sm = _make_security_manager()
        user = _make_user()
        datasource = _make_datasource("query")

        await DatasourceController._call_raise_for_access(
            sm, datasource, DatasourceType.QUERY, user
        )

        sm.raise_for_access.assert_awaited_once_with(query=datasource, user=user)
        # Must NOT have been called with datasource= kwarg (which would take Path 3)
        for c in sm.raise_for_access.call_args_list:
            assert "datasource" not in c.kwargs, (
                "Query type must NOT pass datasource= to raise_for_access"
            )

    @pytest.mark.anyio
    async def test_table_type_passes_datasource_kwarg(self) -> None:
        """datasource_type='table' must route through Path 3 (datasource= kwarg)."""
        sm = _make_security_manager()
        user = _make_user()
        datasource = _make_datasource("table")

        await DatasourceController._call_raise_for_access(
            sm, datasource, DatasourceType.TABLE, user
        )

        sm.raise_for_access.assert_awaited_once_with(datasource=datasource, user=user)
        for c in sm.raise_for_access.call_args_list:
            assert "query" not in c.kwargs, (
                "Table type must NOT pass query= to raise_for_access"
            )

    @pytest.mark.anyio
    async def test_saved_query_type_raises_attribute_error(self) -> None:
        """datasource_type='saved_query' raises AttributeError — 1:1 original.

        Upstream ``datasource.raise_for_access()`` is called on the model
        object (superset_old/datasource/api.py:107), and ``SavedQuery``
        (superset_old/models/sql_lab.py:389) defines NO ``raise_for_access``
        method — the original therefore raises AttributeError (→ 500).
        """
        sm = _make_security_manager()
        user = _make_user()
        datasource = _make_datasource("saved_query")

        with pytest.raises(AttributeError, match="raise_for_access"):
            await DatasourceController._call_raise_for_access(
                sm, datasource, DatasourceType.SAVEDQUERY, user
            )

        sm.raise_for_access.assert_not_awaited()

    @pytest.mark.anyio
    async def test_query_type_string_literal_also_routes_correctly(self) -> None:
        """String literal 'query' (not enum member) still routes to query= path."""
        sm = _make_security_manager()
        user = _make_user()
        datasource = _make_datasource("query")

        # DatasourceType.QUERY == "query" so the comparison still holds
        await DatasourceController._call_raise_for_access(sm, datasource, "query", user)

        sm.raise_for_access.assert_awaited_once_with(query=datasource, user=user)

    @pytest.mark.anyio
    async def test_security_exception_propagates(self) -> None:
        """A SupersetSecurityException raised by the manager propagates up."""
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
        from superset.exceptions import SupersetSecurityException

        err = SupersetError(
            message="no access",
            error_type=SupersetErrorType.TABLE_SECURITY_ACCESS_ERROR,
            level=ErrorLevel.WARNING,
        )
        sm = _make_security_manager()
        sm.raise_for_access.side_effect = SupersetSecurityException(err)
        user = _make_user()
        datasource = _make_datasource("table")

        with pytest.raises(SupersetSecurityException):
            await DatasourceController._call_raise_for_access(
                sm, datasource, DatasourceType.TABLE, user
            )

    @pytest.mark.anyio
    async def test_query_security_exception_propagates(self) -> None:
        """SupersetSecurityException for query type also propagates up."""
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
        from superset.exceptions import SupersetSecurityException

        err = SupersetError(
            message="no query access",
            error_type=SupersetErrorType.TABLE_SECURITY_ACCESS_ERROR,
            level=ErrorLevel.WARNING,
        )
        sm = _make_security_manager()
        sm.raise_for_access.side_effect = SupersetSecurityException(err)
        user = _make_user()
        datasource = _make_datasource("query")

        with pytest.raises(SupersetSecurityException):
            await DatasourceController._call_raise_for_access(
                sm, datasource, DatasourceType.QUERY, user
            )


# ---------------------------------------------------------------------------
# Code-structure check: get_column_values / get_datasource use the helper
#
# We inspect the source to confirm both handlers delegate to
# _call_raise_for_access rather than hardcoding raise_for_access(datasource=…).
# This is intentionally a lightweight structural check; the dispatch logic
# itself is fully covered by TestCallRaiseForAccessDispatch above.
# ---------------------------------------------------------------------------


class TestGetColumnValuesDelegates:
    """Structural checks: handlers use _call_raise_for_access, not hardcoded paths."""

    def test_get_column_values_calls_helper_not_raw_raise_for_access(self) -> None:
        """get_column_values source must call _call_raise_for_access, not raw manager.

        If a future edit reverts to hardcoding ``raise_for_access(datasource=…)``
        the test catches it immediately without needing a full async integration run.
        """
        import inspect

        src = inspect.getsource(DatasourceController.get_column_values.fn)
        assert "_call_raise_for_access" in src, (
            "get_column_values must delegate to _call_raise_for_access"
        )

    def test_get_datasource_calls_helper_not_raw_raise_for_access(self) -> None:
        """get_datasource source must call _call_raise_for_access, not raw manager."""
        import inspect

        src = inspect.getsource(DatasourceController.get_datasource.fn)
        assert "_call_raise_for_access" in src, (
            "get_datasource must delegate to _call_raise_for_access"
        )

    def test_call_raise_for_access_is_a_static_method(self) -> None:
        """_call_raise_for_access must be a staticmethod (callable w/o instance)."""
        assert isinstance(
            DatasourceController.__dict__["_call_raise_for_access"], staticmethod
        )
