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
"""Async port of ``superset_old/commands/dataset/update.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.utils import compute_owner_list
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.tags.core import sync_owner_tags_after_update

if TYPE_CHECKING:
    from superset.db.daos.dataset import AsyncDatasetDAO
    from superset.models.connectors import SqlaTable

logger = logging.getLogger(__name__)


class UpdateDatasetCommand(AsyncBaseCommand["SqlaTable"]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        dataset_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
        override_columns: bool = False,
    ) -> None:
        self._dao = dao
        self._dataset_id = dataset_id
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._override_columns = override_columns
        self._dataset: Any | None = None

    async def validate(self) -> None:  # noqa: C901
        self._dataset = await self._dao.find_by_id(self._dataset_id)
        if not self._dataset:
            raise ObjectNotFoundError("Dataset", self._dataset_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dataset, self._user_id
            )
        # Column semantics — 1:1 with upstream ``_validate_columns``: reject
        # duplicate names, then verify submitted ``id``s belong to THIS dataset
        # (no cross-dataset column-id injection) and — unless ``override_columns``
        # — that new (id-less) column names don't collide with existing ones.
        columns = self._data.get("columns")
        if columns:
            col_names = [
                c.get("column_name") or c.get("name", "")
                for c in columns
                if isinstance(c, dict)
            ]
            seen: set[str] = set()
            for name in col_names:
                if name in seen:
                    raise CommandInvalidError(f"Duplicate column name: '{name}'")
                seen.add(name)
            column_ids = [
                c["id"] for c in columns if isinstance(c, dict) and "id" in c
            ]
            if not await self._dao.validate_columns_exist(
                self._dataset_id, column_ids
            ):
                raise CommandInvalidError(
                    "One or more columns do not exist on this dataset"
                )
            if not self._override_columns:
                new_col_names = [
                    c["column_name"]
                    for c in columns
                    if isinstance(c, dict) and "id" not in c and c.get("column_name")
                ]
                if not await self._dao.validate_columns_uniqueness(
                    self._dataset_id, new_col_names
                ):
                    raise CommandInvalidError(
                        "One or more column names already exist on this dataset"
                    )

        # Metric semantics — 1:1 with upstream ``_validate_metrics`` (no
        # ``override`` flag for metrics).
        metrics = self._data.get("metrics")
        if metrics:
            metric_names = [
                m.get("metric_name") or m.get("name", "")
                for m in metrics
                if isinstance(m, dict)
            ]
            seen_metrics: set[str] = set()
            for name in metric_names:
                if name in seen_metrics:
                    raise CommandInvalidError(f"Duplicate metric name: '{name}'")
                seen_metrics.add(name)
            metric_ids = [
                m["id"] for m in metrics if isinstance(m, dict) and "id" in m
            ]
            if not await self._dao.validate_metrics_exist(
                self._dataset_id, metric_ids
            ):
                raise CommandInvalidError(
                    "One or more metrics do not exist on this dataset"
                )
            new_metric_names = [
                m["metric_name"]
                for m in metrics
                if isinstance(m, dict) and "id" not in m and m.get("metric_name")
            ]
            if not await self._dao.validate_metrics_uniqueness(
                self._dataset_id, new_metric_names
            ):
                raise CommandInvalidError(
                    "One or more metric names already exist on this dataset"
                )

        table_name = self._data.get("table_name")
        if table_name:
            database_id = self._data.get("database_id") or self._dataset.database_id
            is_unique = await self._dao.validate_uniqueness(
                database_id=int(database_id),
                table_name=table_name,
                schema=self._data.get("schema"),
                dataset_id=self._dataset_id,
            )
            if not is_unique:
                raise CommandInvalidError(
                    "Dataset with this table_name/schema/database already exists"
                )

    async def run(self) -> "SqlaTable":
        assert self._dataset is not None

        # Mirrors UpdateDatasetCommand._validate_sql_access in the
        # original Flask Superset: ``if sql and sql != self._model.sql``.
        # Skip when sql is empty (physical table) or unchanged — the
        # dataset edit modal sends sql="" for every save, so checking
        # only ``is not None`` would re-validate access on every PUT
        # and pull in a sync lazy-load of self._dataset.database that
        # crashes with MissingGreenlet under asyncpg.
        sql = self._data.get("sql")
        if sql and sql != self._dataset.sql and self._security_manager is not None:
            from superset.exceptions import SupersetSecurityException

            await self._dao.session.refresh(self._dataset, ["database"])
            database = getattr(self._dataset, "database", None)
            if database:
                # ``raise_for_access`` takes a User OBJECT (``is_admin`` /
                # ``can_access`` read its roles + perms). Passing the bare
                # ``user_id`` int — as before — made every check fall through to
                # "no roles/perms" and silently denied the SQL update for
                # everyone, owner and admin included. Resolve the user like
                # ``CreateDatasetCommand`` does.
                user = (
                    await self._security_manager.find_user_by_id(self._user_id)
                    if self._user_id is not None
                    else None
                )
                schema = self._data.get("schema") or getattr(
                    self._dataset, "schema", None
                )
                try:
                    await self._security_manager.raise_for_access(
                        database=database,
                        schema=schema,
                        sql=sql,
                        user=user,
                    )
                except SupersetSecurityException as exc:
                    raise CommandInvalidError(
                        "Access denied: insufficient SQL access"
                    ) from exc

        # Columns/metrics are handled by DAO.update() special logic
        data = dict(self._data)
        columns = data.pop("columns", None)
        metrics = data.pop("metrics", None)
        owner_ids = data.pop("owners", None)
        update_attrs: dict[str, Any] = {}
        if columns is not None:
            update_attrs["columns"] = [
                dict(c) if not isinstance(c, dict) else c for c in columns
            ]
        if metrics is not None:
            update_attrs["metrics"] = [
                dict(m) if not isinstance(m, dict) else m for m in metrics
            ]
        if self._security_manager is not None:
            await self._dao.session.refresh(self._dataset, ["owners"])
            update_attrs["owners"] = await compute_owner_list(
                self._security_manager,
                self._user_id,
                list(self._dataset.owners),
                owner_ids,
            )
        for key, value in data.items():
            if hasattr(self._dataset, key):
                update_attrs[key] = value
        if self._user_id is not None:
            update_attrs["changed_by_fk"] = self._user_id
        await self._dao.update(self._dataset, update_attrs)
        await self._dao.session.flush()

        # Sync implicit owner: tags (async port of DatasetUpdater.after_update)
        await self._dao.session.refresh(self._dataset, ["owners"])
        owner_ids = (
            [o.id for o in self._dataset.owners]
            if hasattr(self._dataset, "owners")
            else []
        )
        await sync_owner_tags_after_update(
            self._dao.session, "dataset", self._dataset.id, owner_ids
        )

        return self._dataset
