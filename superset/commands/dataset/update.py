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


def validate_folders(
    folders: list[dict[str, Any]],
    metrics: list[Any],
    columns: list[Any],
) -> None:
    """Additional folder-structure validation — 1:1 port of
    ``superset_old/commands/dataset/update.py::validate_folders``.

    Gated on ``DATASET_FOLDERS``; checks valid leaf UUIDs (metric/column),
    sibling-unique + non-reserved names, and absence of cycles.
    """
    from superset.utils.feature_flags import feature_flag_manager

    if not feature_flag_manager.is_feature_enabled("DATASET_FOLDERS"):
        raise CommandInvalidError("Dataset folders are not enabled")

    existing = {
        *[str(getattr(m, "uuid", "")) for m in metrics],
        *[str(getattr(c, "uuid", "")) for c in columns],
    }

    queue: list[tuple[dict[str, Any], list[str]]] = [(f, []) for f in folders]
    seen_uuids: set[str] = set()
    seen_fqns: set[tuple[str, ...]] = set()
    while queue:
        obj, path = queue.pop(0)
        uuid, name = str(obj.get("uuid", "")), obj.get("name")

        if uuid in path:
            raise CommandInvalidError(f"Cycle detected: {uuid} appears in its ancestry")
        if uuid in seen_uuids:
            raise CommandInvalidError(f"Duplicate UUID in folder structure: {uuid}")
        seen_uuids.add(uuid)

        # folders can share a name as long as they're not siblings
        if name:
            fqn = (*path, name)
            if fqn in seen_fqns:
                raise CommandInvalidError(f"Duplicate folder name: {name}")
            seen_fqns.add(fqn)
            if name.lower() in {"metrics", "columns"}:
                raise CommandInvalidError(f"Folder cannot have name '{name}'")
        # a leaf (no name) must reference an existing metric/column UUID
        elif uuid not in existing:
            raise CommandInvalidError(f"Invalid UUID: {uuid}")

        if children := obj.get("children"):
            child_path = [*path, uuid]
            queue.extend((f, child_path) for f in children)


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
        # Not-found (404) and ownership (403) short-circuit immediately — 1:1
        # with upstream (``raise DatasetNotFoundError()`` / ``DatasetForbidden
        # Error``). Every *field* validation below ACCUMULATES into a single
        # ``DatasetInvalidError`` so the controller emits a per-field 422 body.
        from superset.commands.dataset.exceptions import (
            DatabaseNotFoundValidationError,
            DatasetColumnNotFoundValidationError,
            DatasetColumnsDuplicateValidationError,
            DatasetColumnsExistsValidationError,
            DatasetDataAccessIsNotAllowed,
            DatasetExistsValidationError,
            DatasetInvalidError,
            DatasetMetricsDuplicateValidationError,
            DatasetMetricsExistsValidationError,
            DatasetMetricsNotFoundValidationError,
            DatasetValidationError,
            MultiCatalogDisabledValidationError,
        )
        from superset.sql.parse import Table

        self._dataset = await self._dao.find_by_id(self._dataset_id)
        if not self._dataset:
            raise ObjectNotFoundError("Dataset", self._dataset_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dataset, self._user_id
            )

        exceptions: list[DatasetValidationError] = []

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
            if len(col_names) != len(set(col_names)):
                exceptions.append(DatasetColumnsDuplicateValidationError())
            else:
                column_ids = [
                    c["id"] for c in columns if isinstance(c, dict) and "id" in c
                ]
                if not await self._dao.validate_columns_exist(
                    self._dataset_id, column_ids
                ):
                    exceptions.append(DatasetColumnNotFoundValidationError())
                if not self._override_columns:
                    new_col_names = [
                        c["column_name"]
                        for c in columns
                        if isinstance(c, dict)
                        and "id" not in c
                        and c.get("column_name")
                    ]
                    if not await self._dao.validate_columns_uniqueness(
                        self._dataset_id, new_col_names
                    ):
                        exceptions.append(DatasetColumnsExistsValidationError())

        # Metric semantics — 1:1 with upstream ``_validate_metrics`` (no
        # ``override`` flag for metrics).
        metrics = self._data.get("metrics")
        if metrics:
            metric_names = [
                m.get("metric_name") or m.get("name", "")
                for m in metrics
                if isinstance(m, dict)
            ]
            if len(metric_names) != len(set(metric_names)):
                exceptions.append(DatasetMetricsDuplicateValidationError())
            else:
                metric_ids = [
                    m["id"] for m in metrics if isinstance(m, dict) and "id" in m
                ]
                if not await self._dao.validate_metrics_exist(
                    self._dataset_id, metric_ids
                ):
                    exceptions.append(DatasetMetricsNotFoundValidationError())
                new_metric_names = [
                    m["metric_name"]
                    for m in metrics
                    if isinstance(m, dict) and "id" not in m and m.get("metric_name")
                ]
                if not await self._dao.validate_metrics_uniqueness(
                    self._dataset_id, new_metric_names
                ):
                    exceptions.append(DatasetMetricsExistsValidationError())

        # Folder-structure validation — 1:1 with upstream ``_validate_semantics``:
        # only when ``folders`` is provided. Rejects with 422 when DATASET_FOLDERS
        # is disabled (the default), else validates UUIDs/names/cycles. Upstream
        # appends a field-less marshmallow ``ValidationError`` here → normalized
        # under the ``"_schema"`` key; ``DatasetValidationError`` defaults to it.
        folders = self._data.get("folders")
        if folders:
            await self._dao.session.refresh(self._dataset, ["metrics", "columns"])
            try:
                validate_folders(
                    folders,
                    list(getattr(self._dataset, "metrics", []) or []),
                    list(getattr(self._dataset, "columns", []) or []),
                )
            except CommandInvalidError as ex:
                exceptions.append(DatasetValidationError(str(ex)))

        # --- Dataset source: db-change resolution, catalog coercion,
        # uniqueness + SQL-access — 1:1 with upstream ``_validate_dataset_source``
        # / ``_validate_sql_access`` (both run in validate()).  Resolve the
        # target Database explicitly via the DAO/refresh (NOT a sync lazy-load on
        # ``self._dataset.database``) to avoid MissingGreenlet under asyncpg.
        new_db: Any | None = None
        database_id = self._data.get("database_id")
        if database_id and database_id != self._dataset.database_id:
            new_db = await self._dao.get_database_by_id(int(database_id))
            if new_db is None:
                exceptions.append(DatabaseNotFoundValidationError())

        # ``db`` is the connection the dataset will end up on: the resolved new
        # one, else the dataset's current database.
        if new_db is not None:
            db = new_db
        else:
            await self._dao.session.refresh(self._dataset, ["database"])
            db = getattr(self._dataset, "database", None)

        # Catalog validation / coercion — 1:1 with upstream
        # ``_validate_dataset_source`` (MultiCatalogDisabled + default coercion).
        catalog = self._data.get("catalog")
        default_catalog = (
            db.get_default_catalog()
            if db is not None and hasattr(db, "get_default_catalog")
            else None
        )
        allow_multi_catalog = bool(getattr(db, "allow_multi_catalog", False))
        if (
            "catalog" in self._data
            and catalog != default_catalog
            and not allow_multi_catalog
        ):
            exceptions.append(MultiCatalogDisabledValidationError())
        elif db is not None and not allow_multi_catalog:
            catalog = self._data["catalog"] = default_catalog
        elif "catalog" not in self._data:
            catalog = getattr(self._dataset, "catalog", None)

        schema = (
            self._data["schema"]
            if "schema" in self._data
            else getattr(self._dataset, "schema", None)
        )

        # 1:1 with upstream ``_validate_dataset_source``: the uniqueness
        # check ALWAYS runs, falling back to the current model's table_name
        # when the PUT body omits it (``self._properties.get("table_name",
        # self._model.table_name)``) — a schema-only change can still
        # collide with another dataset, and the DB-level UniqueConstraint
        # was dropped (migration df3d7e2eb9a4), so this is the only guard.
        table_name = self._data.get("table_name") or getattr(
            self._dataset, "table_name", None
        )
        if table_name:
            # Use the resolved target db for the uniqueness check (1:1 upstream
            # ``validate_update_uniqueness(db, table, ...)``).
            uniq_db_id = (
                int(getattr(db, "id", 0))
                if db is not None and getattr(db, "id", None) is not None
                else int(self._dataset.database_id)
            )
            # Coalesce catalog to the db default — 1:1 with upstream
            # ``validate_update_uniqueness`` which computes
            # ``table.catalog or database.get_default_catalog()`` before
            # filtering ``SqlaTable.catalog == catalog``.
            uniq_catalog = catalog if catalog is not None else default_catalog
            is_unique = await self._dao.validate_uniqueness(
                database_id=uniq_db_id,
                table_name=table_name,
                schema=schema,
                catalog=uniq_catalog,
                dataset_id=self._dataset_id,
            )
            if not is_unique:
                exceptions.append(
                    DatasetExistsValidationError(Table(table_name, schema, catalog))
                )

        # SQL-access validation — 1:1 with upstream ``_validate_sql_access``:
        # only when ``sql`` is provided AND differs from the current value.
        # Accumulates two per-field ``sql`` errors (DatasetDataAccessIsNotAllowed
        # on SupersetSecurityException, ``Invalid SQL: ...`` on SupersetParseError).
        sql = self._data.get("sql")
        if (
            sql
            and sql != self._dataset.sql
            and self._security_manager is not None
            and db is not None
        ):
            from superset.exceptions import (
                SupersetParseError,
                SupersetSecurityException,
            )

            # ``raise_for_access`` reads roles/perms off a User OBJECT — resolve
            # it like ``CreateDatasetCommand`` (passing the bare id silently
            # denies for everyone).
            user = (
                await self._security_manager.find_user_by_id(self._user_id)
                if self._user_id is not None
                else None
            )
            try:
                await self._security_manager.raise_for_access(
                    database=db,
                    sql=sql,
                    catalog=catalog,
                    schema=schema,
                    user=user,
                )
            except SupersetSecurityException as ex:
                message = ex.error.message if getattr(ex, "error", None) else str(ex)
                exceptions.append(DatasetDataAccessIsNotAllowed(message))
            except SupersetParseError as ex:
                message = ex.error.message if getattr(ex, "error", None) else str(ex)
                exceptions.append(
                    DatasetValidationError(f"Invalid SQL: {message}", field_name="sql")
                )

        # Validate/resolve owners here (not run()) so a bad owner id surfaces as
        # a per-field ``owners`` error in the accumulated 422 — 1:1 with upstream
        # ``update.py::validate`` (compute_owners + append). Resolved list is
        # stashed for run() to reuse.
        if self._security_manager is not None:
            from superset.commands.dataset.exceptions import (
                OwnersNotFoundValidationError,
            )
            from superset.exceptions import (
                OwnersNotFoundValidationError as GenericOwnersNotFoundError,
            )

            await self._dao.session.refresh(self._dataset, ["owners"])
            try:
                self._owners = await compute_owner_list(
                    self._security_manager,
                    self._user_id,
                    list(self._dataset.owners),
                    self._data.get("owners"),
                )
            except GenericOwnersNotFoundError:
                exceptions.append(OwnersNotFoundValidationError())

        if exceptions:
            raise DatasetInvalidError(exceptions=exceptions)

    async def run(self) -> "SqlaTable":
        assert self._dataset is not None

        # SQL-access validation moved to validate() (1:1 with upstream
        # ``_validate_sql_access``), accumulating per-field ``sql`` 422 errors.

        # Columns/metrics are handled by DAO.update() special logic. The
        # ``database_id`` → ``database`` resolution is applied by setattr below
        # (the resolved Database is used in validate()); here we map the id key
        # onto the model attribute. ``catalog`` may have been coerced in
        # validate() and is carried in ``self._data``.
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
            # Reuse the owners resolved+validated during validate() (per-field
            # validated there); fall back to computing here if needed.
            if getattr(self, "_owners", None) is not None:
                update_attrs["owners"] = self._owners
            else:
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
        # 1:1 with superset_old/commands/dataset/update.py:68
        # (``self._properties["override_columns"] = override_columns``) — the
        # DAO's ``update_columns`` reads this flag to pick the
        # delete-all-and-reinsert override path.
        update_attrs["override_columns"] = self._override_columns
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
