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
"""Async port of ``superset_old/commands/security/delete.py``."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import RLSRuleNotFoundError, RuleDeleteFailedError

logger = logging.getLogger(__name__)


class DeleteRLSRuleCommand(AsyncBaseCommand[None]):
    """Bulk-delete Row Level Security filters.

    Async port of
    ``superset_old.commands.security.delete.DeleteRLSRuleCommand`` — the
    original takes ``list[int]`` of model ids and is the only delete
    entry-point. There is no "single delete" command in original
    Superset; the API has only ``DELETE /?q=[ids]``.
    """

    def __init__(self, dao: Any, model_ids: list[int]) -> None:
        self._dao = dao
        self._model_ids = list(model_ids)
        self._models: list[Any] = []

    async def validate(self) -> None:
        self._models = await self._dao.find_by_ids(self._model_ids)
        if not self._models or len(self._models) != len(self._model_ids):
            raise RLSRuleNotFoundError()

    async def run(self) -> None:
        # Mirrors ``superset_old/commands/security/delete.py`` which wraps
        # the transaction with
        # ``@transaction(on_error=partial(on_error, reraise=RuleDeleteFailedError))``.
        try:
            await self._dao.delete(self._models)
            await self._dao.session.flush()
        except SQLAlchemyError as ex:
            logger.exception("Failed to delete RLS rules")
            raise RuleDeleteFailedError() from ex
