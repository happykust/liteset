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
# mypy: ignore-errors
"""Async port of ``superset_old/commands/database/test_connection.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError
from superset.utils import mask_uri_password

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.models.core import Database

logger = logging.getLogger(__name__)


class DatabaseTestConnectionCommand(AsyncBaseCommand[dict[str, Any]]):
    """Test database connectivity.

    Ported 1:1 from superset_old/commands/database/test_connection.py.
    Builds an ephemeral Database model from the payload, resolves
    the URI (including existing model URI decryption for masked URIs),
    and opens an async connection to verify reachability.
    """

    __test__ = False  # prevent pytest collection

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        default_redirect_uri: str | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id
        # Used to start the OAuth2 dance — usually the absolute
        # ``/api/v1/database/oauth2/`` URI of the running Liteset instance.
        self._default_redirect_uri = default_redirect_uri or "/api/v1/database/oauth2/"
        self._model: Database | None = None

    async def validate(self) -> None:
        uri = self._data.get("sqlalchemy_uri")
        if not uri:
            raise CommandInvalidError("sqlalchemy_uri is required for connection test")

        # If a database_name is provided, try to load the existing model
        # so we can decrypt a masked URI back to the real one.
        database_name = self._data.get("database_name")
        if database_name:
            self._model = await self._dao.get_database_by_name(database_name)

    async def run(self) -> dict[str, Any]:  # noqa: C901
        from sqlalchemy.exc import DBAPIError, NoSuchModuleError  # noqa: F401

        from superset.databases.utils import make_url_safe
        from superset.exceptions import (
            DatabaseTestConnectionDriverError,
            DatabaseTestConnectionUnexpectedError,
            OAuth2RedirectError,
            SupersetErrorsException,
        )
        from superset.utils.database import get_async_connection

        uri = self._data.get("sqlalchemy_uri", "")

        # If the URI matches the masked version of an existing model,
        # use the decrypted URI from the model instead.
        if self._model:
            safe_uri = mask_uri_password(str(self._model.sqlalchemy_uri))
            if uri == safe_uri:
                uri = str(self._model.sqlalchemy_uri)

        # Parse URL into pieces for error context (hostname, port, etc.).
        # Used by engine_spec.extract_errors() to produce SIP-40 error
        # responses.  Matches superset_old/commands/database/test_connection.py:79-89
        url = make_url_safe(uri)
        context = {
            "hostname": url.host,
            "password": url.password,
            "port": url.port,
            "username": url.username,
            "database": url.database,
        }

        # Build an ephemeral Database model for the connection test
        database = self._dao.build_db_for_connection_test(
            server_cert=self._data.get("server_cert", ""),
            extra=self._data.get("extra", "{}"),
            impersonate_user=self._data.get("impersonate_user", False),
            encrypted_extra=self._data.get("masked_encrypted_extra", "{}"),
        )
        database.sqlalchemy_uri = uri

        try:
            async with get_async_connection(database) as (conn, engine_spec):
                # Run a simple ``SELECT 1`` to verify connectivity
                from sqlalchemy import text

                await conn.execute(text("SELECT 1"))

            return {"message": "OK"}

        except (NoSuchModuleError, ModuleNotFoundError) as ex:
            raise DatabaseTestConnectionDriverError(
                message=(
                    f"Could not load database driver: "
                    f"{database.db_engine_spec.__name__}"
                ),
            ) from ex
        except OAuth2RedirectError:
            # ``start_oauth2_dance`` raises this — let it propagate so the
            # frontend can launch the OAuth2 popup.
            raise
        except SupersetErrorsException:
            raise
        except Exception as ex:
            # If the connection failed because OAuth2 is needed, start
            # the dance.  Mirrors test_connection.py:213-217 in the
            # original.
            if (
                self._user_id is not None
                and database.is_oauth2_enabled()
                and database.db_engine_spec.needs_oauth2(ex)
            ):
                await database.db_engine_spec.start_oauth2_dance(
                    database,
                    user_id=self._user_id,
                    default_redirect_uri=self._default_redirect_uri,
                )
            # Delegate to engine spec for structured SIP-40 errors
            # (CONNECTION_INVALID_HOSTNAME_ERROR, CONNECTION_ACCESS_DENIED_ERROR,
            # etc.).  Matches test_connection.py:184-193 — except the
            # original catches DBAPIError specifically because sync
            # SQLAlchemy wraps driver errors.  In async with asyncpg,
            # exceptions raised during pool checkout (e.g.
            # InvalidPasswordError) are NOT wrapped in DBAPIError, so
            # we catch Exception and let extract_errors pattern-match
            # the message.
            #
            # NOTE: liteset's extract_errors returns list[dict] while
            # the original returns list[SupersetError].  We pass the
            # dicts through as-is — they are already SIP-40 shaped.
            errors = database.db_engine_spec.extract_errors(ex, context)
            if errors:
                raise SupersetErrorsException(
                    errors=errors,
                    status_code=400,
                    message=errors[0].get("message", str(ex)),
                ) from ex
            # No custom_errors pattern matched — treat as unexpected
            logger.exception("Unexpected error during connection test")
            raise DatabaseTestConnectionUnexpectedError(
                errors=[
                    {
                        "message": (
                            "Unexpected error occurred, please check your "
                            "logs for details"
                        ),
                        "error_type": "GENERIC_DB_ENGINE_ERROR",
                        "level": "error",
                        "extra": {},
                    }
                ],
                status_code=422,
                message=str(ex),
            ) from ex
