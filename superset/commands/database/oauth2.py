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
"""Async port of ``superset_old/commands/database/oauth2.py``."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, cast, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import (
    CommandInvalidError,
    OAuth2Error,
    ObjectNotFoundError,
)
from superset.utils.oauth2 import decode_oauth2_state, OAuth2State

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseUserOAuth2TokensDAO
    from superset.models.core import Database, DatabaseUserOAuth2Tokens

logger = logging.getLogger(__name__)


class OAuth2StoreTokenCommand(AsyncBaseCommand["DatabaseUserOAuth2Tokens"]):
    """Store OAuth2 tokens in ``database_user_oauth2_tokens``.

    Mirrors the behaviour of the original sync command:

    * Decode the ``state`` parameter to recover the originating database
      and user.
    * Exchange the authorization ``code`` for an access/refresh token
      pair using the engine spec's ``get_oauth2_token`` (async, httpx).
    * Replace any pre-existing token for the same ``(user, database)``
      pair so a second OAuth2 dance always wins.
    """

    def __init__(
        self,
        dao: "AsyncDatabaseUserOAuth2TokensDAO",
        parameters: dict[str, Any],
    ) -> None:
        self._dao = dao
        self._parameters = parameters
        self._state: OAuth2State | None = None
        self._database: "Database" | None = None

    async def validate(self) -> None:
        # Provider-side error short-circuits everything.
        if error := self._parameters.get("error"):
            raise OAuth2Error(str(error))

        if not self._parameters.get("state"):
            raise CommandInvalidError("Missing OAuth2 'state' parameter")
        if not self._parameters.get("code"):
            raise CommandInvalidError("Missing OAuth2 'code' parameter")

        decoded = decode_oauth2_state(self._parameters["state"])
        self._state = cast(OAuth2State, decoded)

        database = await self._dao.get_database(int(self._state["database_id"]))
        if database is None:
            raise ObjectNotFoundError("Database", self._state["database_id"])
        self._database = database

    async def run(self) -> "DatabaseUserOAuth2Tokens":
        assert self._database is not None
        assert self._state is not None

        oauth2_config = self._database.get_oauth2_config()
        if oauth2_config is None:
            raise OAuth2Error("No configuration found for OAuth2")

        # Exchange the authorization code for tokens.
        token_response = await self._database.db_engine_spec.get_oauth2_token(
            oauth2_config,
            self._parameters["code"],
        )

        if "access_token" not in token_response:
            raise OAuth2Error(str(token_response.get("error", "Token exchange failed")))

        # Replace any pre-existing tokens so a second dance always wins.
        if existing := await self._dao.find_one_or_none(
            user_id=int(self._state["user_id"]),
            database_id=int(self._state["database_id"]),
        ):
            await self._dao.delete(existing)

        expires_in = int(token_response.get("expires_in", 0))
        expiration = datetime.now() + timedelta(seconds=expires_in)

        token = await self._dao.create(
            {
                "user_id": int(self._state["user_id"]),
                "database_id": int(self._state["database_id"]),
                "access_token": token_response["access_token"],
                "access_token_expiration": expiration,
                "refresh_token": token_response.get("refresh_token"),
            }
        )
        # Flush so the row has its id populated for any caller reading it
        # (matches the rest of the create commands).
        await self._dao.session.flush()
        return token
