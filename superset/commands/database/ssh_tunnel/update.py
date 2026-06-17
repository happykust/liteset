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
"""Command for updating an SSH tunnel for a database connection.

Validates private-key/password mutual exclusion and database-port presence,
and clears conflicting credential fields when the caller flips between
credential modes.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.database.ssh_tunnel.exceptions import (
    SSHTunnelDatabasePortError,
    SSHTunnelInvalidError,
    SSHTunnelNotFoundError,
    SSHTunnelRequiredFieldValidationError,
    SSHTunnelUpdateFailedError,
)
from superset.databases.utils import make_url_safe
from superset.utils.ssh_tunnel import get_default_port

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncSSHTunnelDAO

logger = logging.getLogger(__name__)


class UpdateSSHTunnelCommand(AsyncBaseCommand[Any]):
    """Update an existing SSH tunnel."""

    def __init__(self, dao: AsyncSSHTunnelDAO, model_id: int, data: dict[str, Any]):
        self._dao = dao
        self._model_id = model_id
        self._properties = data.copy()
        self._model: Any = None

    async def validate(self) -> None:
        # AsyncSSHTunnelDAO only exposes database-keyed lookups; use session.get.
        from superset.models.ssh_tunnel import SSHTunnel

        self._model = await self._dao.session.get(SSHTunnel, self._model_id)
        if not self._model:
            raise SSHTunnelNotFoundError()

        url = make_url_safe(self._model.database.sqlalchemy_uri)
        private_key: Optional[str] = self._properties.get("private_key")
        private_key_password: Optional[str] = self._properties.get(
            "private_key_password"
        )
        if private_key_password and private_key is None:
            raise SSHTunnelInvalidError(
                exceptions=[SSHTunnelRequiredFieldValidationError("private_key")]
            )
        backend = url.get_backend_name()
        port = url.port or get_default_port(backend)
        if not port:
            raise SSHTunnelDatabasePortError()

    async def run(self) -> Any:
        if self._model is None:
            return None

        try:
            if self._properties.get("private_key"):
                self._properties["password"] = None

            if self._properties.get("password"):
                self._properties["private_key"] = None
                self._properties["private_key_password"] = None

            # Delegate to the DAO so ``unmask_password_info`` runs before
            # attributes are written, preventing PASSWORD_MASK from clobbering
            # stored secrets.
            return await self._dao.update(self._model, self._properties)
        except (SSHTunnelDatabasePortError, SSHTunnelInvalidError):
            raise
        except Exception as ex:
            logger.exception("Failed to update SSH tunnel")
            raise SSHTunnelUpdateFailedError() from ex
