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
"""R13-01: the database importer must pass ``user=`` to ``can_access``.

``AsyncSecurityManager.can_access(permission_name, view_name, *, user)`` has a
keyword-only ``user`` parameter; calling it without one raises TypeError (the
same bug class as R12-01 theme_import and R12-02 dashboard importer).
"""

import io
from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.database.importers.v1 import ImportDatabasesCommand
from superset.exceptions import CommandInvalidError
from superset.utils.core import set_current_user


class _StrictSecurityManager:
    """Mimics AsyncSecurityManager.can_access's keyword-only signature."""

    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def can_access(self, permission_name: str, view_name: str, *, user) -> bool:
        self.calls.append(
            {"permission": permission_name, "view": view_name, "user": user}
        )
        return self.result


@pytest.fixture(autouse=True)
def _reset_current_user():
    yield
    set_current_user(None)


async def test_import_single_passes_user_to_can_access():
    user = MagicMock()
    set_current_user(user)

    dao = MagicMock()
    dao.find_one_or_none = AsyncMock(return_value=None)
    sm = _StrictSecurityManager(result=False)

    cmd = ImportDatabasesCommand(
        io.BytesIO(b""),
        dao=dao,
        security_manager=sm,
        ignore_permissions=False,
    )

    # can_access(user=...) → False, database doesn't exist → upstream raises
    # the "no permission to create" error. Without the fix this is a
    # TypeError (missing keyword-only argument 'user') instead.
    with pytest.raises(CommandInvalidError):
        await cmd._import_single(
            "databases/db.yaml", {"database_name": "x", "sqlalchemy_uri": "sqlite://"}
        )

    assert sm.calls
    assert sm.calls[0]["user"] is user


async def test_import_single_denies_without_user_in_context():
    set_current_user(None)

    dao = MagicMock()
    dao.find_one_or_none = AsyncMock(return_value=None)
    sm = _StrictSecurityManager(result=True)

    cmd = ImportDatabasesCommand(
        io.BytesIO(b""),
        dao=dao,
        security_manager=sm,
        ignore_permissions=False,
    )

    # No user in context → deny (can_write False) without even calling
    # can_access — like upstream where an anonymous g.user fails the check.
    with pytest.raises(CommandInvalidError):
        await cmd._import_single(
            "databases/db.yaml", {"database_name": "x", "sqlalchemy_uri": "sqlite://"}
        )
    assert not sm.calls
