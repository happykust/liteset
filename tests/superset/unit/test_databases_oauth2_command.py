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
"""Liteset port of ``tests/unit_tests/commands/databases/oauth2_test.py``.

Adapted to the async ``OAuth2StoreTokenCommand`` which takes an injected
async DAO + the OAuth2 provider response parameters, decodes the ``state``
itself, and replaces any pre-existing token via ``find_one_or_none`` /
``delete`` / ``create``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.database.oauth2 import OAuth2StoreTokenCommand
from superset.exceptions import (
    CommandInvalidError,
    OAuth2Error,
    ObjectNotFoundError,
)
from superset.models.core import Database
from superset.utils.oauth2 import encode_oauth2_state


@pytest.fixture
def mock_database() -> MagicMock:
    database = MagicMock(spec=Database)
    database.get_oauth2_config.return_value = {
        "client_id": "test",
        "client_secret": "secret",
    }
    database.db_engine_spec.get_oauth2_token = AsyncMock(
        return_value={
            "access_token": "test_access_token",
            "expires_in": 3600,
            "refresh_token": "test_refresh_token",
        }
    )
    return database


@pytest.fixture
def mock_state() -> str:
    return encode_oauth2_state(
        {
            "user_id": 1,
            "database_id": 123,
            "default_redirect_uri": "http://localhost:8088/api/v1/oauth2/",
            "tab_id": "1234",
        }
    )


@pytest.fixture
def mock_parameters(mock_state: str) -> dict[str, Any]:
    return {"code": "test_code", "state": mock_state}


def make_dao(database: Any = None) -> AsyncMock:
    """An async OAuth2 tokens DAO mock with the methods the command calls."""
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.flush = AsyncMock()
    dao.get_database = AsyncMock(return_value=database)
    dao.find_one_or_none = AsyncMock(return_value=None)
    dao.delete = AsyncMock()
    dao.create = AsyncMock(return_value="new_token")
    return dao


async def test_validate_success(
    mock_database: MagicMock,
    mock_state: str,
    mock_parameters: dict[str, Any],
) -> None:
    dao = make_dao(mock_database)
    command = OAuth2StoreTokenCommand(dao, mock_parameters)
    await command.validate()

    assert command._database == mock_database
    assert command._state["database_id"] == 123
    assert command._state["user_id"] == 1
    dao.get_database.assert_awaited_once_with(123)


async def test_validate_database_not_found(
    mock_parameters: dict[str, Any],
) -> None:
    dao = make_dao(None)
    command = OAuth2StoreTokenCommand(dao, mock_parameters)
    with pytest.raises(ObjectNotFoundError, match="Database"):
        await command.validate()


async def test_validate_oauth2_error(mock_parameters: dict[str, Any]) -> None:
    mock_parameters["error"] = "OAuth2 failure"
    dao = make_dao()
    command = OAuth2StoreTokenCommand(dao, mock_parameters)
    with pytest.raises(OAuth2Error, match="Something went wrong while doing OAuth2"):
        await command.validate()


async def test_validate_missing_state(mock_parameters: dict[str, Any]) -> None:
    del mock_parameters["state"]
    dao = make_dao()
    command = OAuth2StoreTokenCommand(dao, mock_parameters)
    with pytest.raises(CommandInvalidError, match="state"):
        await command.validate()


async def test_run_success(
    mock_database: MagicMock,
    mock_parameters: dict[str, Any],
) -> None:
    dao = make_dao(mock_database)
    command = OAuth2StoreTokenCommand(dao, mock_parameters)
    await command.validate()
    result = await command.run()

    assert result == "new_token"
    dao.create.assert_awaited_once()
    dao.delete.assert_not_awaited()


async def test_run_existing_token(
    mock_database: MagicMock,
    mock_parameters: dict[str, Any],
) -> None:
    dao = make_dao(mock_database)
    existing_token = MagicMock()
    dao.find_one_or_none = AsyncMock(return_value=existing_token)

    command = OAuth2StoreTokenCommand(dao, mock_parameters)
    await command.validate()
    result = await command.run()

    assert result == "new_token"
    dao.delete.assert_awaited_once_with(existing_token)
    dao.create.assert_awaited_once()
