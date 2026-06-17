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
"""Command for creating an SSH tunnel for a database connection.

Validates server address / port / username + credentials (plus database-port
presence) and raises structured errors for missing fields or mismatched
credential modes.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.database.ssh_tunnel.exceptions import (
    SSHTunnelCreateFailedError,
    SSHTunnelDatabasePortError,
    SSHTunnelInvalidError,
    SSHTunnelRequiredFieldValidationError,
)
from superset.databases.utils import make_url_safe
from superset.events import event_logger
from superset.utils.ssh_tunnel import get_default_port

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncSSHTunnelDAO

logger = logging.getLogger(__name__)


class CreateSSHTunnelCommand(AsyncBaseCommand[Any]):
    """Create an SSH tunnel for a database."""

    def __init__(self, dao: AsyncSSHTunnelDAO, database: Any, data: dict[str, Any]):
        self._dao = dao
        self._database = database
        self._properties = data.copy()
        self._properties["database"] = database
        self._properties["database_id"] = getattr(database, "id", None)

    async def validate(self) -> None:
        # TODO(hughhh): check to make sure the server port is not localhost
        # using the config.SSH_TUNNEL_MANAGER

        exceptions: list[Exception] = []
        server_address: Optional[str] = self._properties.get("server_address")
        server_port: Optional[int] = self._properties.get("server_port")
        username: Optional[str] = self._properties.get("username")
        password: Optional[str] = self._properties.get("password")
        private_key: Optional[str] = self._properties.get("private_key")
        private_key_password: Optional[str] = self._properties.get(
            "private_key_password"
        )
        url = make_url_safe(self._database.sqlalchemy_uri)
        backend = url.get_backend_name()
        port = url.port or get_default_port(backend)
        if not port:
            raise SSHTunnelDatabasePortError()
        if not server_address:
            exceptions.append(SSHTunnelRequiredFieldValidationError("server_address"))
        if not server_port:
            exceptions.append(SSHTunnelRequiredFieldValidationError("server_port"))
        if not username:
            exceptions.append(SSHTunnelRequiredFieldValidationError("username"))
        if not private_key and not password:
            exceptions.append(SSHTunnelRequiredFieldValidationError("password"))
        if private_key_password and private_key is None:
            exceptions.append(SSHTunnelRequiredFieldValidationError("private_key"))
        if exceptions:
            exception = SSHTunnelInvalidError()
            exception.extend(exceptions)
            try:
                if hasattr(exception, "get_list_classnames"):
                    suffix = ".".join(exception.get_list_classnames())
                else:
                    suffix = ".".join(type(e).__name__ for e in exceptions)
                event_logger.log_with_context(
                    action=(
                        f"ssh_tunnel_creation_failed."
                        f"{exception.__class__.__name__}.{suffix}"
                    )
                )
            except Exception:  # noqa: BLE001  # audit logging must never break validation
                logger.exception("Failed to emit ssh_tunnel_creation_failed event")
            raise exception

    async def run(self) -> Any:
        try:
            from superset.models.ssh_tunnel import SSHTunnel

            tunnel = SSHTunnel(
                **{
                    k: v
                    for k, v in self._properties.items()
                    # ``database`` is the ORM relationship; strip it since
                    # ``database_id`` is already set for SA resolution.
                    if k != "database"
                }
            )
            self._dao.session.add(tunnel)
            await self._dao.session.flush()
            return tunnel
        except SSHTunnelDatabasePortError:
            raise
        except SSHTunnelInvalidError:
            raise
        except Exception as ex:
            logger.exception("Failed to create SSH tunnel")
            raise SSHTunnelCreateFailedError() from ex
