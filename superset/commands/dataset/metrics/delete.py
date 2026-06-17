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
"""Command to delete a metric from a dataset."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.dataset import AsyncDatasetDAO, AsyncDatasetMetricDAO


class DeleteDatasetMetricCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dataset_dao: AsyncDatasetDAO,
        metric_dao: AsyncDatasetMetricDAO,
        dataset_id: int,
        metric_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dataset_dao = dataset_dao
        self._metric_dao = metric_dao
        self._dataset_id = dataset_id
        self._metric_id = metric_id
        self._security_manager = security_manager
        self._user_id = user_id
        self._metric: Any | None = None

    async def validate(self) -> None:
        # ``SqlMetric`` has no ``owners`` relationship, so raise_for_ownership
        # denies every non-admin — effectively admin-only; dataset owners manage
        # metrics through ``PUT /dataset/{pk}``.
        self._metric = await self._metric_dao.find_by_dataset_and_id(
            self._dataset_id, self._metric_id
        )
        if not self._metric:
            raise ObjectNotFoundError("DatasetMetric", self._metric_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._metric, self._user_id
            )

    async def run(self) -> None:
        assert self._metric is not None
        await self._metric_dao.session.delete(self._metric)
        await self._metric_dao.session.flush()
