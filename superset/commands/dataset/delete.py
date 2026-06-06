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
"""Async port of ``superset_old/commands/dataset/delete.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.tags.core import delete_tagged_objects

if TYPE_CHECKING:
    from superset.db.daos.dataset import AsyncDatasetDAO

logger = logging.getLogger(__name__)


class DeleteDatasetCommand(AsyncBaseCommand[None]):
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

    async def validate(self) -> None:  # noqa: C901
        self._dataset = await self._dao.find_by_id(self._dataset_id)
        if not self._dataset:
            raise ObjectNotFoundError("Dataset", self._dataset_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dataset, self._user_id
            )

    async def run(self) -> None:
        assert self._dataset is not None
        dataset_id = self._dataset.id
        # Remove implicit tags before deleting
        # (async port of DatasetUpdater.after_delete)
        await delete_tagged_objects(self._dao.session, "dataset", dataset_id)
        await self._dao.session.delete(self._dataset)
        await self._dao.session.flush()


class BulkDeleteDatasetsCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        dataset_ids: list[int],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dataset_ids = dataset_ids
        self._security_manager = security_manager
        self._user_id = user_id
        self._datasets: list[Any] = []

    async def validate(self) -> None:
        if not self._dataset_ids:
            raise CommandInvalidError("No dataset IDs provided")
        self._datasets = await self._dao.find_by_ids(self._dataset_ids)
        found_ids = {int(d.id) for d in self._datasets}
        missing = set(self._dataset_ids) - found_ids
        if missing:
            raise ObjectNotFoundError("Dataset", str(sorted(missing)))
        if self._security_manager is not None:
            for dataset in self._datasets:
                await self._security_manager.raise_for_ownership(dataset, self._user_id)

    async def run(self) -> None:
        for dataset in self._datasets:
            # Remove implicit tags before deleting each dataset — 1:1 with
            # upstream ``DatasetDAO.delete(models)`` which fires the per-model
            # ``DatasetUpdater.after_delete`` SQLAlchemy event
            # (``delete_tagged_objects``). The single-delete command already
            # does this; the bulk path was dropping the cleanup.
            await delete_tagged_objects(self._dao.session, "dataset", dataset.id)
            await self._dao.session.delete(dataset)
        await self._dao.session.flush()
