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
"""Unit tests for RLS controller, commands and schemas.

Validation contract:

* msgspec performs field-level validation (``name``/``clause`` non-empty,
  ``filter_type`` in {Regular, Base}) — *not* the command.
* Commands resolve and validate referenced ``tables`` / ``roles`` against
  the database, raising :class:`DatasourceNotFoundValidationError` /
  :class:`RolesNotFoundValidationError` (both 422).
* ``DeleteRLSRuleCommand`` is the single delete entry-point and accepts
  ``list[int]`` of model ids — no separate single-delete command.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from superset.commands.security.create import CreateRLSRuleCommand
from superset.commands.security.delete import DeleteRLSRuleCommand
from superset.commands.security.update import UpdateRLSRuleCommand
from superset.controllers.rls import RLSController
from superset.exceptions import (
    DatasourceNotFoundValidationError,
    RLSRuleNotFoundError,
    RolesNotFoundValidationError,
)
from superset.schemas.rls import RLSPostSchema, RLSPutSchema


@pytest.fixture
def mock_dao() -> AsyncMock:
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    dao.session.execute = AsyncMock()
    return dao


def _execute_returning(values: list[Any]) -> AsyncMock:
    res = MagicMock()
    res.scalars.return_value.all.return_value = values
    return AsyncMock(return_value=res)


def test_rls_controller_path() -> None:
    assert RLSController.path == "/api/v1/rowlevelsecurity"


def test_rls_controller_tags() -> None:
    assert RLSController.tags == ["Row Level Security"]


async def test_create_rls_resolves_tables_and_roles(mock_dao: AsyncMock) -> None:
    table_a = MagicMock(id=10)
    table_b = MagicMock(id=11)
    role_x = MagicMock(id=1)

    mock_dao.session.execute = AsyncMock(
        side_effect=[
            _execute_returning([role_x]).return_value,
            _execute_returning([table_a, table_b]).return_value,
        ]
    )
    mock_dao.create = AsyncMock(return_value=MagicMock(id=42))

    cmd = CreateRLSRuleCommand(
        dao=mock_dao,
        data={
            "name": "Test",
            "clause": "client_id = 1",
            "filter_type": "Regular",
            "tables": [10, 11],
            "roles": [1],
        },
    )
    await cmd.validate()
    item = await cmd.run()

    create_args = mock_dao.create.await_args.args[0]
    assert create_args["tables"] == [table_a, table_b]
    assert create_args["roles"] == [role_x]
    assert item.id == 42


async def test_create_rls_raises_when_table_missing(mock_dao: AsyncMock) -> None:
    mock_dao.session.execute = AsyncMock(
        side_effect=[
            _execute_returning([]).return_value,
            _execute_returning([MagicMock(id=10)]).return_value,
        ]
    )

    cmd = CreateRLSRuleCommand(
        dao=mock_dao,
        data={
            "name": "Test",
            "clause": "1=1",
            "filter_type": "Regular",
            "tables": [10, 11],
            "roles": [],
        },
    )
    with pytest.raises(DatasourceNotFoundValidationError):
        await cmd.validate()


async def test_create_rls_raises_when_role_missing(mock_dao: AsyncMock) -> None:
    mock_dao.session.execute = _execute_returning([])

    cmd = CreateRLSRuleCommand(
        dao=mock_dao,
        data={
            "name": "Test",
            "clause": "1=1",
            "filter_type": "Regular",
            "tables": [10],
            "roles": [99],
        },
    )
    with pytest.raises(RolesNotFoundValidationError):
        await cmd.validate()


async def test_update_rls_not_found(mock_dao: AsyncMock) -> None:
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateRLSRuleCommand(dao=mock_dao, model_id=999, data={"name": "X"})
    with pytest.raises(RLSRuleNotFoundError):
        await cmd.validate()


async def test_update_rls_replaces_both_collections(
    mock_dao: AsyncMock,
) -> None:
    """Both ``tables`` and ``roles`` are always replaced (full-replace semantics,
    not partial-patch).

    When the PUT omits ``tables``, the command defaults it to ``[]`` and
    replaces the collection with an empty list.  The port previously kept stale
    tables when the key was absent — this test verifies the fix.
    """
    existing = MagicMock(id=1)
    mock_dao.find_by_id = AsyncMock(return_value=existing)
    role = MagicMock(id=2)
    # First execute(): populate_roles (Roles); second execute(): tables (empty list
    # because tables not supplied → _tables=[] → no IN query → execute not called
    # for tables).  populate_roles calls execute for Role lookup.
    mock_dao.session.execute = _execute_returning([role])
    mock_dao.update = AsyncMock(return_value=existing)

    cmd = UpdateRLSRuleCommand(
        dao=mock_dao, model_id=1, data={"name": "Updated", "roles": [2]}
    )
    await cmd.validate()
    await cmd.run()

    update_args = mock_dao.update.await_args.args
    assert update_args[0] is existing
    payload = update_args[1]
    assert payload["roles"] == [role]
    assert payload["tables"] == []


async def test_delete_rls_not_found(mock_dao: AsyncMock) -> None:
    mock_dao.find_by_ids = AsyncMock(return_value=[])
    cmd = DeleteRLSRuleCommand(dao=mock_dao, model_ids=[1, 2])
    with pytest.raises(RLSRuleNotFoundError):
        await cmd.validate()


async def test_delete_rls_partial_match_raises(mock_dao: AsyncMock) -> None:
    mock_dao.find_by_ids = AsyncMock(return_value=[MagicMock(id=1)])
    cmd = DeleteRLSRuleCommand(dao=mock_dao, model_ids=[1, 2])
    with pytest.raises(RLSRuleNotFoundError):
        await cmd.validate()


async def test_delete_rls_success(mock_dao: AsyncMock) -> None:
    items = [MagicMock(id=1), MagicMock(id=2)]
    mock_dao.find_by_ids = AsyncMock(return_value=items)
    mock_dao.delete = AsyncMock()

    cmd = DeleteRLSRuleCommand(dao=mock_dao, model_ids=[1, 2])
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with(items)


_BASE_POST = {
    "name": "Test",
    "filter_type": "Regular",
    "clause": "1=1",
    "tables": [10],
    "roles": [],
}


def test_rls_post_schema_valid() -> None:
    body = msgspec.convert(_BASE_POST, RLSPostSchema)
    assert body.name == "Test"
    assert body.tables == [10]
    assert body.roles == []
    # Optional fields absent from the input must be UNSET (not None) so
    # that filter_unset() can exclude them from the create-response result,
    # matching Marshmallow 3 Schema.load() which omits absent optional fields.
    assert body.description is msgspec.UNSET
    assert body.group_key is msgspec.UNSET


def test_rls_post_schema_roles_required() -> None:
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert(
            {k: v for k, v in _BASE_POST.items() if k != "roles"},
            RLSPostSchema,
        )


def test_rls_post_schema_name_required() -> None:
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({**_BASE_POST, "name": ""}, RLSPostSchema)


def test_rls_post_schema_clause_required() -> None:
    """``clause`` is required (``required=True`` in the original
    marshmallow schema) but has *no* min-length validator — empty
    string is a valid clause.
    """
    # Missing key -> rejected
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert(
            {k: v for k, v in _BASE_POST.items() if k != "clause"},
            RLSPostSchema,
        )
    # Empty string -> accepted (matches original behaviour)
    body = msgspec.convert({**_BASE_POST, "clause": ""}, RLSPostSchema)
    assert body.clause == ""


def test_rls_post_schema_description_allows_none() -> None:
    """``description`` has ``allow_none=True`` in the original schema."""
    body = msgspec.convert({**_BASE_POST, "description": None}, RLSPostSchema)
    assert body.description is None


def test_rls_post_schema_group_key_allows_none() -> None:
    """``group_key`` has ``allow_none=True`` in the original schema."""
    body = msgspec.convert({**_BASE_POST, "group_key": None}, RLSPostSchema)
    assert body.group_key is None


def test_rls_post_schema_tables_required_min_length_1() -> None:
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({**_BASE_POST, "tables": []}, RLSPostSchema)


def test_rls_post_schema_filter_type_oneof() -> None:
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({**_BASE_POST, "filter_type": "Invalid"}, RLSPostSchema)


def test_rls_post_schema_name_max_length() -> None:
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({**_BASE_POST, "name": "x" * 256}, RLSPostSchema)


def test_rls_post_schema_absent_optional_fields_are_unset() -> None:
    """Absent optional fields produce UNSET, not None.

    This is the key invariant: filter_unset() can then strip them from the
    create-response payload to match the Marshmallow 3 Schema.load() contract
    (original Superset only includes keys that were present in the request).
    """
    body = msgspec.convert(_BASE_POST, RLSPostSchema)
    assert body.description is msgspec.UNSET
    assert body.group_key is msgspec.UNSET


def test_create_response_result_excludes_absent_optional_fields() -> None:
    """POST result must NOT include ``description``/``group_key`` when absent.

    Original Superset (Marshmallow 3): ``item = schema.load(request.json)``
    only returns keys that were present in the input.  Sending
    ``{"name":"t", "clause":"1=1", "filter_type":"Regular",
    "tables":[1], "roles":[]}`` yields a result without ``description`` or
    ``group_key``.

    Regression: _msgspec_to_dict iterated all __struct_fields__ (including
    defaulted-None description/group_key) so those keys always appeared.
    Fix: RLSPostSchema uses UNSET for optional fields + filter_unset() in
    create().
    """
    from superset.controllers.rls import _msgspec_to_dict
    from superset.utils import filter_unset

    body = msgspec.convert(_BASE_POST, RLSPostSchema)
    result = filter_unset(_msgspec_to_dict(body))

    assert "description" not in result
    assert "group_key" not in result
    assert result["name"] == "Test"
    assert result["clause"] == "1=1"
    assert result["tables"] == [10]


def test_create_response_result_includes_explicit_null_optional_fields() -> None:
    """Explicitly-null optional fields MUST appear in the result.

    When the client sends ``"description": null``, the result must include
    ``"description": null`` — Marshmallow 3 includes null values that are
    explicitly provided (``allow_none=True``).
    """
    from superset.controllers.rls import _msgspec_to_dict
    from superset.utils import filter_unset

    body = msgspec.convert({**_BASE_POST, "description": None}, RLSPostSchema)
    result = filter_unset(_msgspec_to_dict(body))

    assert "description" in result
    assert result["description"] is None
    assert "group_key" not in result  # still absent


def test_rls_put_schema_all_unset() -> None:
    body = msgspec.convert({}, RLSPutSchema)
    assert body.name is msgspec.UNSET
    assert body.clause is msgspec.UNSET
    assert body.filter_type is msgspec.UNSET


def test_rls_put_schema_partial() -> None:
    body = msgspec.convert({"name": "Updated"}, RLSPutSchema)
    assert body.name == "Updated"
    assert body.clause is msgspec.UNSET


def test_rls_put_schema_invalid_filter_type() -> None:
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"filter_type": "Bogus"}, RLSPutSchema)
