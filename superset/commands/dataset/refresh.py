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
"""Command to refresh a dataset's column and metric metadata."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.dataset import AsyncDatasetDAO
    from superset.models.connectors import SqlaTable

logger = logging.getLogger(__name__)


class RefreshDatasetCommand(AsyncBaseCommand["SqlaTable"]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        dataset_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dataset_id = dataset_id
        self._security_manager = security_manager
        self._user_id = user_id
        self._dataset: Any | None = None

    async def validate(self) -> None:
        self._dataset = await self._dao.find_by_id(self._dataset_id)
        if not self._dataset:
            raise ObjectNotFoundError("Dataset", self._dataset_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dataset, self._user_id
            )

    async def run(self) -> "SqlaTable":
        assert self._dataset is not None
        # SQLAlchemy introspection failures → DatasetRefreshFailedError (422).
        # Non-SQLAlchemy errors (e.g. SupersetGenericDBErrorException from virtual
        # datasets) propagate unchanged with their own status code.
        try:
            await self._dao.fetch_metadata(self._dataset)
        except SQLAlchemyError as ex:
            from superset.commands.dataset.exceptions import (
                DatasetRefreshFailedError,
            )

            logger.warning("fetch_metadata failed on dataset refresh", exc_info=True)
            raise DatasetRefreshFailedError(exceptions=[ex]) from ex
        return self._dataset
