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
"""Command for duplicating a virtual dataset."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand

if TYPE_CHECKING:
    from superset.db.daos.dataset import AsyncDatasetDAO
    from superset.models.connectors import SqlaTable


class DuplicateDatasetCommand(AsyncBaseCommand["SqlaTable"]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        base_model_id: int,
        table_name: str,
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._base_model_id = base_model_id
        self._table_name = table_name
        self._user_id = user_id
        self._security_manager = security_manager
        self._source: Any | None = None

    async def validate(self) -> None:
        from superset.commands.dataset.exceptions import (
            DatasetExistsValidationError,
            DatasetInvalidError,
            DatasetValidationError,
            DatasourceTypeInvalidError,
        )
        from superset.sql.parse import Table

        # Access-scoped: every sibling command (refresh/delete/update) calls
        # ``raise_for_ownership``; this one previously used the DAO's
        # unfiltered ``find_by_id``, so any authenticated user could copy a
        # dataset — SQL included — they cannot otherwise see, landing the
        # copy in their own ownership.
        if self._security_manager is not None and self._user_id is not None:
            from superset.db.filters import dataset_access_filters
            from superset.models.connectors import SqlaTable

            user = await self._security_manager.find_user_by_id(self._user_id)
            self._source = None
            if user is not None:
                access_filters = await dataset_access_filters(
                    self._security_manager, user
                )
                results = await self._dao.find_all(
                    filters=[SqlaTable.id == self._base_model_id, *access_filters],
                    page=0,
                    page_size=1,
                )
                self._source = results[0] if results else None
        else:
            self._source = await self._dao.find_by_id(self._base_model_id)

        exceptions: list[DatasetValidationError] = []
        if not self._source:
            exceptions.append(
                DatasetValidationError(
                    "Dataset does not exist", field_name="base_model_id"
                )
            )
        if not self._table_name:
            exceptions.append(
                DatasetValidationError(
                    "table_name is required", field_name="table_name"
                )
            )

        if self._source and not getattr(self._source, "sql", None):
            exceptions.append(DatasourceTypeInvalidError())

        existing = await self._dao.find_one_or_none(table_name=self._table_name)
        if existing is not None:
            exceptions.append(DatasetExistsValidationError(Table(self._table_name)))

        if exceptions:
            raise DatasetInvalidError(exceptions=exceptions)

    async def run(self) -> "SqlaTable":
        from superset.models.connectors import SqlaTable, SqlMetric, TableColumn

        assert self._source is not None
        source_sql = getattr(self._source, "sql", None)
        if source_sql:
            source_sql = source_sql.strip().strip(";")
        new_dataset = SqlaTable(
            table_name=self._table_name,
            database_id=self._source.database_id,
            schema=getattr(self._source, "schema", None),
            sql=source_sql,
            description=getattr(self._source, "description", None),
            is_sqllab_view=True,
        )
        if self._user_id is not None:
            new_dataset.created_by_fk = self._user_id
            new_dataset.changed_by_fk = self._user_id
            from superset.models.security import User

            current_user = await self._dao.session.get(User, self._user_id)
            if current_user is not None:
                new_dataset.owners = [current_user]
        # Pre-init collections before session.add so SQLAlchemy treats them as
        # already-loaded; otherwise the first .append() triggers a sync lazy-load
        # against asyncpg → MissingGreenlet (lazy="select" fires on first touch
        # even on a transient instance).
        new_dataset.columns = []
        new_dataset.metrics = []

        self._dao.session.add(new_dataset)
        await self._dao.session.flush()

        await self._dao.session.refresh(self._source, ["columns", "metrics"])

        if hasattr(self._source, "columns"):
            for col in self._source.columns:
                new_col = TableColumn(
                    column_name=col.column_name,
                    verbose_name=getattr(col, "verbose_name", None),
                    type=getattr(col, "type", None),
                    # Duplicated columns always default to groupby=True, filterable=True
                    # regardless of source flags.
                    groupby=True,
                    filterable=True,
                    description=getattr(col, "description", None),
                    is_dttm=getattr(col, "is_dttm", False),
                    expression=getattr(col, "expression", None),
                    python_date_format=getattr(col, "python_date_format", None),
                    table_id=new_dataset.id,
                )
                new_dataset.columns.append(new_col)

        if hasattr(self._source, "metrics"):
            for metric in self._source.metrics:
                new_metric = SqlMetric(
                    metric_name=metric.metric_name,
                    expression=metric.expression,
                    metric_type=getattr(metric, "metric_type", None),
                    description=getattr(metric, "description", None),
                    verbose_name=getattr(metric, "verbose_name", None),
                    table_id=new_dataset.id,
                )
                new_dataset.metrics.append(new_metric)

        new_dataset.template_params = getattr(  # type: ignore[assignment]
            self._source, "template_params", None
        )
        new_dataset.normalize_columns = getattr(  # type: ignore[assignment]
            self._source, "normalize_columns", False
        )
        new_dataset.always_filter_main_dttm = getattr(  # type: ignore[assignment]
            self._source, "always_filter_main_dttm", False
        )

        await self._dao.session.flush()
        return new_dataset
