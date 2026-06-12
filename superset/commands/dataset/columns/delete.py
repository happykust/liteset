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
"""Async port of ``superset_old/commands/dataset/columns/delete.py``."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.dataset import AsyncDatasetColumnDAO, AsyncDatasetDAO


class DeleteDatasetColumnCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dataset_dao: AsyncDatasetDAO,
        column_dao: AsyncDatasetColumnDAO,
        dataset_id: int,
        column_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dataset_dao = dataset_dao
        self._column_dao = column_dao
        self._dataset_id = dataset_id
        self._column_id = column_id
        self._security_manager = security_manager
        self._user_id = user_id
        self._column: Any | None = None

    async def validate(self) -> None:
        # 1:1 with upstream ``DeleteDatasetColumnCommand.validate``: resolve
        # the column scoped to the dataset FIRST (missing dataset or column →
        # 404 ``DatasetColumnNotFoundError``), THEN check ownership on the
        # COLUMN itself.  ``TableColumn`` has no ``owners`` relationship, so
        # ``raise_for_ownership`` denies every non-admin — effectively
        # admin-only, exactly like upstream (R14-07); dataset owners manage
        # columns through ``PUT /dataset/{pk}`` instead.
        self._column = await self._column_dao.find_by_dataset_and_id(
            self._dataset_id, self._column_id
        )
        if not self._column:
            raise ObjectNotFoundError("DatasetColumn", self._column_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._column, self._user_id
            )

    async def run(self) -> None:
        assert self._column is not None
        await self._column_dao.session.delete(self._column)
        await self._column_dao.session.flush()
