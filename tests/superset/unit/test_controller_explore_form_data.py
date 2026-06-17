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
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import PermissionDeniedException

from superset.commands.explore_permalink.utils import check_dataset_access
from superset.controllers.explore_form_data import ExploreFormDataController
from superset.exceptions import ForbiddenError, ObjectNotFoundError


async def test_controller_path():
    assert ExploreFormDataController.path == "/api/v1/explore/form_data"


async def test_resource():
    assert ExploreFormDataController.resource == "explore_form_data"


async def test_check_dataset_access_eager_loads_relationships():
    """The dataset is loaded with ``owners`` + ``database`` eager-loaded so the
    downstream ``can_access_datasource`` never triggers a sync lazy-load
    (``MissingGreenlet``) on the async session — the bug that turned
    ``POST /api/v1/explore/form_data`` into a 500 for any user lacking
    ``all_datasource_access``.
    """
    dao = AsyncMock()
    dao.find_by_id_with_options = AsyncMock(return_value=MagicMock())
    sm = AsyncMock()
    sm.can_access_datasource = AsyncMock(return_value=True)

    result = await check_dataset_access(dao, 1, security_manager=sm, user=MagicMock())

    assert result is True
    dao.find_by_id_with_options.assert_awaited_once()
    args, _ = dao.find_by_id_with_options.call_args
    assert args[0] == 1
    # Exactly two eager-load options: selectinload(owners) + selectinload(database)
    # so that can_access_datasource never triggers a sync lazy-load (MissingGreenlet).
    assert len(args[1]) == 2
    dao.find_by_id.assert_not_called()


async def test_check_dataset_access_denied_raises_forbidden():
    dao = AsyncMock()
    dao.find_by_id_with_options = AsyncMock(return_value=MagicMock())
    sm = AsyncMock()
    sm.can_access_datasource = AsyncMock(return_value=False)

    with pytest.raises(ForbiddenError):
        await check_dataset_access(dao, 1, security_manager=sm, user=MagicMock())


async def test_check_dataset_access_missing_raises_not_found():
    dao = AsyncMock()
    dao.find_by_id_with_options = AsyncMock(return_value=None)
    sm = AsyncMock()

    with pytest.raises(ObjectNotFoundError):
        await check_dataset_access(dao, 1, security_manager=sm, user=MagicMock())


def _make_envelope(owner: int | None) -> str:
    return {
        "owner": owner,
        "datasource_id": 1,
        "datasource_type": "table",
        "chart_id": None,
        "form_data": '{"viz_type": "table"}',
    }


def _controller_self() -> MagicMock:
    self_mock = MagicMock()
    self_mock.resource = ExploreFormDataController.resource
    return self_mock


async def test_update_value_owner_none_raises_permission_denied():
    """PUT with a None-owner entry must raise PermissionDeniedException
    when the caller is an authenticated user (id != None).

    Regression for: ``if owner is not None and owner != current_user.id``
    short-circuiting to False when owner=None (should raise, matches
    original ``state["owner"] != get_user_id()`` → ``None != 456`` → True).
    """
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=_make_envelope(owner=None))

    user = MagicMock()
    user.id = 456

    data = MagicMock()
    data.form_data = '{"viz_type": "table"}'
    data.datasource_id = 1
    data.datasource_type = "table"
    data.chart_id = None

    update_fn = ExploreFormDataController.update_value.fn
    with (
        patch("superset.controllers.explore_form_data.check_access", new=AsyncMock()),
        patch(
            "superset.controllers.explore_form_data._form_data_cache",
            return_value=cache,
        ),
    ):
        with pytest.raises(PermissionDeniedException):
            await update_fn(
                _controller_self(),
                request=MagicMock(),
                key="some-key",
                data=data,
                chart_dao=AsyncMock(),
                dataset_dao=AsyncMock(),
                query_dao=AsyncMock(),
                security_manager=AsyncMock(),
                current_user=user,
                tab_id=None,
            )


async def test_update_value_owner_mismatch_raises_permission_denied():
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=_make_envelope(owner=99))

    user = MagicMock()
    user.id = 456

    data = MagicMock()
    data.form_data = '{"viz_type": "table"}'
    data.datasource_id = 1
    data.datasource_type = "table"
    data.chart_id = None

    update_fn = ExploreFormDataController.update_value.fn
    with (
        patch("superset.controllers.explore_form_data.check_access", new=AsyncMock()),
        patch(
            "superset.controllers.explore_form_data._form_data_cache",
            return_value=cache,
        ),
    ):
        with pytest.raises(PermissionDeniedException):
            await update_fn(
                _controller_self(),
                request=MagicMock(),
                key="some-key",
                data=data,
                chart_dao=AsyncMock(),
                dataset_dao=AsyncMock(),
                query_dao=AsyncMock(),
                security_manager=AsyncMock(),
                current_user=user,
                tab_id=None,
            )


async def test_delete_value_owner_none_raises_permission_denied():
    """DELETE with a None-owner entry must raise PermissionDeniedException
    when the caller is authenticated.

    Regression for: ``if owner is not None and owner != current_user.id``
    short-circuiting to False when owner=None (should raise, matches
    original ``state["owner"] != get_user_id()`` → ``None != 456`` → True).
    """
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=_make_envelope(owner=None))

    user = MagicMock()
    user.id = 456

    delete_fn = ExploreFormDataController.delete_value.fn
    with (
        patch("superset.controllers.explore_form_data.check_access", new=AsyncMock()),
        patch(
            "superset.controllers.explore_form_data._form_data_cache",
            return_value=cache,
        ),
    ):
        with pytest.raises(PermissionDeniedException):
            await delete_fn(
                _controller_self(),
                request=MagicMock(),
                key="some-key",
                chart_dao=AsyncMock(),
                dataset_dao=AsyncMock(),
                query_dao=AsyncMock(),
                security_manager=AsyncMock(),
                current_user=user,
            )


async def test_delete_value_owner_mismatch_raises_permission_denied():
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=_make_envelope(owner=99))

    user = MagicMock()
    user.id = 456

    delete_fn = ExploreFormDataController.delete_value.fn
    with (
        patch("superset.controllers.explore_form_data.check_access", new=AsyncMock()),
        patch(
            "superset.controllers.explore_form_data._form_data_cache",
            return_value=cache,
        ),
    ):
        with pytest.raises(PermissionDeniedException):
            await delete_fn(
                _controller_self(),
                request=MagicMock(),
                key="some-key",
                chart_dao=AsyncMock(),
                dataset_dao=AsyncMock(),
                query_dao=AsyncMock(),
                security_manager=AsyncMock(),
                current_user=user,
            )


async def test_get_value_non_dict_entry_raises_not_found():
    """A non-dict cache entry (corrupt/legacy — the create path always stores a
    TemporaryExploreState dict) must NOT be returned without an access check;
    it is treated as not found instead of being leaked verbatim."""
    cache = MagicMock()
    cache.get = AsyncMock(return_value="corrupt-non-dict-entry")

    get_fn = ExploreFormDataController.get_value.fn
    with patch(
        "superset.controllers.explore_form_data._form_data_cache",
        return_value=cache,
    ):
        with pytest.raises(ObjectNotFoundError):
            await get_fn(
                _controller_self(),
                key="some-key",
                chart_dao=AsyncMock(),
                dataset_dao=AsyncMock(),
                query_dao=AsyncMock(),
                security_manager=AsyncMock(),
                current_user=MagicMock(),
            )
