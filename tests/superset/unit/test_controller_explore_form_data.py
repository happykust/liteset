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
"""Tests for ExploreFormDataController."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.explore_permalink.utils import check_dataset_access
from superset.controllers.explore_form_data import ExploreFormDataController
from superset.exceptions import ForbiddenError, ObjectNotFoundError


async def test_controller_path():
    assert ExploreFormDataController.path == "/api/v1/explore/form_data"


async def test_resource():
    assert ExploreFormDataController.resource == "explore_form_data"


# ---------------------------------------------------------------------------
# check_dataset_access — eager-load guard (regression for the form_data 500)
# ---------------------------------------------------------------------------


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
    assert len(args[1]) == 2  # selectinload(owners) + selectinload(database)
    # The bare (lazy) find_by_id must NOT be used on this path.
    dao.find_by_id.assert_not_called()


async def test_check_dataset_access_denied_raises_forbidden():
    """A user without datasource access gets ForbiddenError (→ 403), not a 500."""
    dao = AsyncMock()
    dao.find_by_id_with_options = AsyncMock(return_value=MagicMock())
    sm = AsyncMock()
    sm.can_access_datasource = AsyncMock(return_value=False)

    with pytest.raises(ForbiddenError):
        await check_dataset_access(dao, 1, security_manager=sm, user=MagicMock())


async def test_check_dataset_access_missing_raises_not_found():
    """A missing dataset raises ObjectNotFoundError (→ 404)."""
    dao = AsyncMock()
    dao.find_by_id_with_options = AsyncMock(return_value=None)
    sm = AsyncMock()

    with pytest.raises(ObjectNotFoundError):
        await check_dataset_access(dao, 1, security_manager=sm, user=MagicMock())
