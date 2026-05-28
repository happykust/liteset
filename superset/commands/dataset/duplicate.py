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
"""Async port of ``superset_old/commands/dataset/duplicate.py``."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

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
    ) -> None:
        self._dao = dao
        self._base_model_id = base_model_id
        self._table_name = table_name
        self._user_id = user_id
        self._source: Any | None = None

    async def validate(self) -> None:
        if not self._table_name:
            raise CommandInvalidError("table_name is required")
        self._source = await self._dao.find_by_id(self._base_model_id)
        if not self._source:
            raise ObjectNotFoundError("Dataset", self._base_model_id)

        # Only virtual datasets (with SQL) can be duplicated
        if not getattr(self._source, "sql", None):
            raise CommandInvalidError("Only virtual datasets can be duplicated")

        # Check that the new name doesn't already exist. Mirrors
        # ``superset_old/commands/dataset/duplicate.py:118``: the original
        # rejects the name if a dataset with that ``table_name`` exists in ANY
        # database (``DatasetDAO.find_one_or_none(table_name=...)``), not just
        # the source database/schema.
        existing = await self._dao.find_one_or_none(table_name=self._table_name)
        if existing is not None:
            raise CommandInvalidError(
                f"Dataset with name '{self._table_name}' already exists"
            )

    async def run(self) -> "SqlaTable":
        from superset.models.connectors import SqlaTable, SqlMetric, TableColumn

        assert self._source is not None
        source_sql = getattr(self._source, "sql", None)
        if source_sql:
            source_sql = source_sql.strip().rstrip(";")
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
            # Mirrors ``superset_old/commands/dataset/duplicate.py:122`` which
            # passes ``owners=self.populate_owners()`` to ``SqlaTable(...)``;
            # ``CreateMixin.populate_owners`` defaults to the current user
            # (``default_to_user=True``) when no owner ids are supplied.
            from superset.models.security import User

            current_user = await self._dao.session.get(User, self._user_id)
            if current_user is not None:
                new_dataset.owners = [current_user]
        # Initialise the *new* dataset's collections BEFORE ``session.add``
        # so SQLAlchemy registers them as already-loaded — otherwise a
        # subsequent ``.append(...)`` (or any read access) triggers a sync
        # lazy-load against asyncpg and dies with ``MissingGreenlet``. The
        # default ``lazy="select"`` strategy fires the SELECT eagerly on
        # first attribute touch even for an obviously-empty collection on a
        # transient instance.
        new_dataset.columns = []
        new_dataset.metrics = []

        self._dao.session.add(new_dataset)
        await self._dao.session.flush()

        # Eagerly load source relationships before copying
        await self._dao.session.refresh(self._source, ["columns", "metrics"])

        # Copy columns
        if hasattr(self._source, "columns"):
            for col in self._source.columns:
                new_col = TableColumn(
                    column_name=col.column_name,
                    type=getattr(col, "type", None),
                    groupby=getattr(col, "groupby", True),
                    filterable=getattr(col, "filterable", True),
                    description=getattr(col, "description", None),
                    is_dttm=getattr(col, "is_dttm", False),
                    expression=getattr(col, "expression", None),
                    python_date_format=getattr(col, "python_date_format", None),
                    table_id=new_dataset.id,
                )
                new_dataset.columns.append(new_col)

        # Copy metrics
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

        # Copy additional fields
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
