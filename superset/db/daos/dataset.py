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
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from superset.db.base_dao import BaseAsyncDAO
from superset.models.connectors import SqlaTable, SqlMetric, TableColumn
from superset.models.core import Database
from superset.models.dashboard import Dashboard, dashboard_slices
from superset.models.slice import Slice


class AsyncDatasetDAO(BaseAsyncDAO[SqlaTable]):
    model_cls = SqlaTable

    async def get_database_by_id(self, database_id: int) -> Database | None:
        """Get a Database by its ID."""
        return await self.session.get(Database, database_id)

    async def find_by_id_with_options(
        self,
        dataset_id: int,
        options: list[Any] | None = None,
    ) -> SqlaTable | None:
        """Find a dataset by id with optional eager-load ``options``.

        Used when the caller needs to serialize relationship collections
        (``database``, ``columns``, ``metrics``, ``owners``, …) in the
        same async context to avoid ``MissingGreenlet`` errors on lazy
        relationship access under asyncpg.
        """
        stmt = select(SqlaTable).where(SqlaTable.id == dataset_id)
        if options:
            stmt = stmt.options(*options)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def validate_uniqueness(
        self,
        database_id: int,
        table_name: str,
        schema: str | None = None,
        catalog: str | None = None,
        dataset_id: int | None = None,
    ) -> bool:
        """Check that no dataset exists with the given name/schema/database combo."""
        stmt = select(SqlaTable).where(
            SqlaTable.table_name == table_name,
            SqlaTable.database_id == database_id,
        )
        if schema is not None:
            stmt = stmt.where(SqlaTable.schema == schema)
        if catalog is not None:
            stmt = stmt.where(SqlaTable.catalog == catalog)
        if dataset_id is not None:
            stmt = stmt.where(SqlaTable.id != dataset_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def validate_columns_exist(
        self,
        dataset_id: int,
        column_ids: list[int],
    ) -> bool:
        """Verify that all column IDs belong to the given dataset."""
        if not column_ids:
            return True
        stmt = select(TableColumn.id).where(
            TableColumn.table_id == dataset_id,
            TableColumn.id.in_(column_ids),
        )
        result = await self.session.execute(stmt)
        found = set(result.scalars().all())
        return found >= set(column_ids)

    async def validate_metrics_exist(
        self,
        dataset_id: int,
        metric_ids: list[int],
    ) -> bool:
        """Verify that all metric IDs belong to the given dataset."""
        if not metric_ids:
            return True
        stmt = select(SqlMetric.id).where(
            SqlMetric.table_id == dataset_id,
            SqlMetric.id.in_(metric_ids),
        )
        result = await self.session.execute(stmt)
        found = set(result.scalars().all())
        return found >= set(metric_ids)

    async def validate_columns_uniqueness(
        self,
        dataset_id: int,
        columns_names: list[str],
    ) -> bool:
        """Check for duplicate column names in a dataset.

        Returns True if none of the given column names already exist
        on the dataset.

        Ports the original ``DatasetDAO.validate_columns_uniqueness`` logic.
        """
        if not columns_names:
            return True
        stmt = select(TableColumn.id).where(
            TableColumn.table_id == dataset_id,
            TableColumn.column_name.in_(columns_names),
        )
        result = await self.session.execute(stmt)
        return len(list(result.scalars().all())) == 0

    async def validate_metrics_uniqueness(
        self,
        dataset_id: int,
        metrics_names: list[str],
    ) -> bool:
        """Check for duplicate metric names in a dataset.

        Returns True if none of the given metric names already exist
        on the dataset.

        Ports the original ``DatasetDAO.validate_metrics_uniqueness`` logic.
        """
        if not metrics_names:
            return True
        stmt = select(SqlMetric.id).where(
            SqlMetric.table_id == dataset_id,
            SqlMetric.metric_name.in_(metrics_names),
        )
        result = await self.session.execute(stmt)
        return len(list(result.scalars().all())) == 0

    async def update(
        self,
        item: SqlaTable,
        attributes: dict[str, Any],
    ) -> SqlaTable:
        """Update dataset with special handling for columns and metrics."""
        attributes = {**attributes}
        force_update = False

        # Handle relationship reassignments BEFORE columns/metrics so the
        # subsequent autoflush triggered by update_columns/update_metrics
        # doesn't lazy-load the old relationship under sync setattr —
        # which would crash with MissingGreenlet under asyncpg.
        for rel_key in ("owners", "tags"):
            if rel_key in attributes:
                new_value = attributes.pop(rel_key)
                await self.session.refresh(item, [rel_key])
                setattr(item, rel_key, new_value)

        if "columns" in attributes:
            await self.update_columns(item, attributes.pop("columns"))
            force_update = True

        if "metrics" in attributes:
            await self.update_metrics(item, attributes.pop("metrics"))
            force_update = True

        if force_update:
            attributes["changed_on"] = datetime.now()

        return await super().update(item, attributes)

    async def update_columns(
        self,
        model: SqlaTable,
        property_columns: list[dict[str, Any]],
    ) -> None:
        """Update dataset columns: insert new, update existing, delete removed."""
        await self.session.refresh(model, ["columns"])
        existing_columns = {col.id: col for col in model.columns}

        incoming_ids = set()
        for col_data in property_columns:
            col_data = dict(col_data)  # avoid mutating caller's data
            col_id = col_data.get("id")
            if col_id and col_id in existing_columns:
                col = existing_columns[col_id]
                for key, value in col_data.items():
                    if key != "id":
                        setattr(col, key, value)
                incoming_ids.add(col_id)
            else:
                col_data["table_id"] = model.id
                col_data.pop("id", None)
                new_col = TableColumn(**col_data)
                model.columns.append(new_col)

        ids_to_delete = set(existing_columns.keys()) - incoming_ids
        if ids_to_delete:
            for cid in ids_to_delete:
                model.columns.remove(existing_columns[cid])
                await self.session.delete(existing_columns[cid])

    async def update_metrics(
        self,
        model: SqlaTable,
        property_metrics: list[dict[str, Any]],
    ) -> None:
        """Update dataset metrics: insert new, update existing, delete removed."""
        await self.session.refresh(model, ["metrics"])
        existing_metrics = {m.id: m for m in model.metrics}

        incoming_ids = set()
        for metric_data in property_metrics:
            metric_data = dict(metric_data)  # avoid mutating caller's data
            metric_id = metric_data.get("id")
            if metric_id and metric_id in existing_metrics:
                metric = existing_metrics[metric_id]
                for key, value in metric_data.items():
                    if key != "id":
                        setattr(metric, key, value)
                incoming_ids.add(metric_id)
            else:
                metric_data["table_id"] = model.id
                metric_data.pop("id", None)
                new_metric = SqlMetric(**metric_data)
                model.metrics.append(new_metric)

        ids_to_delete = set(existing_metrics.keys()) - incoming_ids
        if ids_to_delete:
            for mid in ids_to_delete:
                model.metrics.remove(existing_metrics[mid])
                await self.session.delete(existing_metrics[mid])

    async def get_related_objects(
        self,
        dataset_id: int,
    ) -> dict[str, list[Any]]:
        """Get charts and dashboards related to a dataset."""
        chart_stmt = select(Slice).where(
            Slice.datasource_id == dataset_id,
            Slice.datasource_type == "table",
        )
        chart_result = await self.session.execute(chart_stmt)
        charts = list(chart_result.scalars().all())

        chart_ids = [c.id for c in charts]
        dashboards: list[Any] = []
        if chart_ids:
            dash_stmt = (
                select(Dashboard)
                .join(dashboard_slices, Dashboard.id == dashboard_slices.c.dashboard_id)
                .where(dashboard_slices.c.slice_id.in_(chart_ids))
                .distinct()
            )
            dash_result = await self.session.execute(dash_stmt)
            dashboards = list(dash_result.scalars().all())

        return {"charts": charts, "dashboards": dashboards}


class AsyncDatasetColumnDAO(BaseAsyncDAO[TableColumn]):
    model_cls = TableColumn

    async def find_by_dataset_and_id(
        self,
        dataset_id: int,
        column_id: int,
    ) -> TableColumn | None:
        """Find a column by dataset and column ID."""
        return await self.find_one_or_none(table_id=dataset_id, id=column_id)


class AsyncDatasetMetricDAO(BaseAsyncDAO[SqlMetric]):
    model_cls = SqlMetric

    async def find_by_dataset_and_id(
        self,
        dataset_id: int,
        metric_id: int,
    ) -> SqlMetric | None:
        """Find a metric by dataset and metric ID."""
        return await self.find_one_or_none(table_id=dataset_id, id=metric_id)
