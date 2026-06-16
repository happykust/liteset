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
"""Regression tests for R13-07 — DatabaseFilter visibility scope on by-id
database endpoints.

Upstream applies ``DatabaseFilter`` as the DAO ``base_filter`` on EVERY by-id
operation (superset_old/daos/database.py:38 + daos/base.py:52-92, FAB
``base_filters`` at databases/api.py:187): a database outside the user's
visibility scope behaves exactly like a missing one — 404, no metadata, no
mutation.  The liteset port had the equivalent in-Python gate
(``_database_is_accessible``) on only 3 endpoints; the other by-pk endpoints
did an unscoped ``find_by_id``, letting any user with the class-level
permission read connection details / related objects / function names of —
or modify — databases they cannot access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.controllers.database import DatabaseController
from superset.exceptions import ObjectNotFoundError


def _fn(name: str):
    handler = getattr(DatabaseController, name)
    return handler.fn if hasattr(handler, "fn") else handler


def _common():
    controller = DatabaseController(owner=MagicMock())
    dao = MagicMock()
    dao.find_by_id = AsyncMock(return_value=MagicMock())  # database EXISTS
    sm = MagicMock()
    user = MagicMock()
    return controller, dao, sm, user


# Handler name -> extra kwargs beyond (pk, dao, security_manager, current_user)
_ENDPOINTS: dict[str, dict[str, object]] = {
    "get_connection": {},
    "update": {"request": MagicMock()},
    "delete_database": {},
    "sync_permissions": {},
    "catalogs": {"q": None},
    "tables": {"rison_params": {"schema_name": "public"}},
    "related_objects": {},
    "validate_sql": {"data": MagicMock()},
    "function_names": {},
    "schemas_access_for_file_upload": {},
    "upload": {"data": {}},
    "delete_ssh_tunnel": {},
}


@pytest.mark.parametrize("name", sorted(_ENDPOINTS))
async def test_by_id_endpoint_hides_inaccessible_database(name: str) -> None:
    """An existing database the user cannot see must 404 — 1:1 with the
    upstream DatabaseFilter base_filter — instead of serving/mutating it."""
    controller, dao, sm, user = _common()
    with patch(
        "superset.controllers.database._database_is_accessible",
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(ObjectNotFoundError):
            await _fn(name)(
                controller,
                pk=1,
                dao=dao,
                security_manager=sm,
                current_user=user,
                **_ENDPOINTS[name],
            )


async def test_export_hides_inaccessible_database() -> None:
    """Export takes ids via rison — any inaccessible id must 404 the export
    (upstream: scoped find_by_ids → count mismatch → DatabaseNotFoundError)."""
    controller, dao, sm, user = _common()
    with patch(
        "superset.controllers.database._database_is_accessible",
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(ObjectNotFoundError):
            await _fn("export")(
                controller,
                dao=dao,
                rison_params=[1],
                security_manager=sm,
                current_user=user,
                token=None,
            )


async def test_accessible_database_passes_gate() -> None:
    """A visible database proceeds normally (no spurious 404)."""
    controller, dao, sm, user = _common()
    dao.get_related_objects = AsyncMock(
        return_value={"charts": [], "dashboards": [], "sqllab_tab_states": []}
    )
    with patch(
        "superset.controllers.database._database_is_accessible",
        new=AsyncMock(return_value=True),
    ):
        result = await _fn("related_objects")(
            controller, pk=1, dao=dao, security_manager=sm, current_user=user
        )
    assert result["charts"]["count"] == 0
    assert result["dashboards"]["count"] == 0


async def test_missing_database_still_404s() -> None:
    """A genuinely missing database 404s before the accessibility check."""
    controller, dao, sm, user = _common()
    dao.find_by_id = AsyncMock(return_value=None)
    accessible = AsyncMock(return_value=True)
    with patch("superset.controllers.database._database_is_accessible", new=accessible):
        with pytest.raises(ObjectNotFoundError):
            await _fn("related_objects")(
                controller, pk=99, dao=dao, security_manager=sm, current_user=user
            )
    accessible.assert_not_awaited()
