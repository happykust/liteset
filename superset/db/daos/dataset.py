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

import dateutil.parser
from sqlalchemy import select

from superset.db.base_dao import BaseAsyncDAO
from superset.models.connectors import (
    MetadataResult,
    SqlaTable,
    SqlMetric,
    TableColumn,
)
from superset.models.core import Database
from superset.models.dashboard import Dashboard, dashboard_slices
from superset.models.slice import Slice


def _apply_sqla_table_mutator(model: SqlaTable) -> None:
    """Apply the configured ``SQLA_TABLE_MUTATOR`` to a dataset (default no-op).

    Mirrors ``current_app.config["SQLA_TABLE_MUTATOR"](self)`` from the original
    ``SqlaTable.fetch_metadata``. Resolution follows the same dual-discovery
    path used elsewhere (``mutate_sql_based_on_config``): legacy uppercase
    constant first, then the Pydantic settings attribute.
    """
    try:
        from superset import config as _config
    except ImportError:
        return
    mutator = getattr(_config, "SQLA_TABLE_MUTATOR", None)
    if mutator is None:
        try:
            settings = _config.SupersetSettings()  # type: ignore[call-arg]
            mutator = getattr(settings, "sqla_table_mutator", None)
        except Exception:  # noqa: BLE001, S110
            pass
    if mutator:
        mutator(model)


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
        """Check that no dataset exists with the given name/schema/database combo.

        1:1 with ``superset_old/daos/dataset.py::validate_uniqueness`` /
        ``validate_update_uniqueness``: ``schema`` and ``catalog`` are filtered
        UNCONDITIONALLY (``== None`` → ``IS NULL``) so two datasets sharing a
        name in *different* catalogs/schemas do not collide. The caller is
        responsible for coalescing ``catalog`` to the database default (the
        original does so inside this method via ``table.catalog or
        database.get_default_catalog()``; the async commands resolve it from the
        Database object before calling, since the DAO only has ``database_id``).
        """
        stmt = select(SqlaTable).where(
            SqlaTable.table_name == table_name,
            SqlaTable.database_id == database_id,
            SqlaTable.schema == schema,
            SqlaTable.catalog == catalog,
        )
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

    @staticmethod
    def validate_python_date_format(dt_format: str) -> bool:
        """1:1 with ``superset_old/daos/dataset.py:validate_python_date_format``.

        A ``python_date_format`` is valid when it is either the literal
        ``epoch_s`` / ``epoch_ms`` sentinel, or a strftime format whose
        rendered output parses back as an ISO datetime.
        """
        if dt_format in ("epoch_s", "epoch_ms"):
            return True
        try:
            dt_str = datetime.now().strftime(dt_format)
            dateutil.parser.isoparse(dt_str)
            return True
        except ValueError:
            return False

    async def update_columns(
        self,
        model: SqlaTable,
        property_columns: list[dict[str, Any]],
    ) -> None:
        """Update dataset columns: insert new, update existing, delete removed."""
        # 1:1 with upstream ``DatasetDAO.update_columns``
        # (``superset_old/daos/dataset.py:222-232``): every supplied
        # ``python_date_format`` is validated up front, before any persist,
        # raising ``ValueError`` on the first invalid format.
        for column in property_columns:
            if (
                "python_date_format" in column
                and column["python_date_format"] is not None
            ):
                if not self.validate_python_date_format(
                    column["python_date_format"]
                ):
                    raise ValueError(
                        "python_date_format is an invalid date/timestamp format."
                    )

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

    async def fetch_metadata(self, model: SqlaTable) -> MetadataResult:
        """Introspect table columns + metrics and merge them onto the dataset.

        Async port of ``SqlaTable.fetch_metadata`` in
        ``superset_old/connectors/sqla/models.py`` (line 1699). The original is
        synchronous and ends with ``db.session.merge(self)``; here the blocking
        introspection (``external_metadata`` + ``Database.get_metrics``) runs in
        a thread while the ORM column diff, collection mutation and persistence
        happen on the async session. ``database``/``columns``/``metrics`` are
        eager-refreshed first so neither the threaded introspection nor the
        relationship diff triggers a lazy load under asyncpg (``MissingGreenlet``).
        The caller owns the surrounding transaction (commit), matching create /
        refresh which flush within the request-scoped session.

        Returns the :class:`MetadataResult` diff (added / removed / modified
        column names), 1:1 with the original.
        """
        import asyncio

        from superset.sql.parse import Table as ParsedTable

        await self.session.refresh(model, ["database", "columns", "metrics"])

        db_engine_spec = model.db_engine_spec

        new_columns = await asyncio.to_thread(model.external_metadata)
        parsed_table = ParsedTable(
            model.table_name,
            model.schema or None,
            model.catalog or None,
        )
        metric_dicts = await asyncio.to_thread(
            model.database.get_metrics, parsed_table
        )
        metrics = [SqlMetric(**metric) for metric in metric_dicts]

        any_date_col: str | None = None
        old_columns = list(model.columns)
        old_columns_by_name = {col.column_name: col for col in old_columns}

        new_column_names = {col["column_name"] for col in new_columns}
        results = MetadataResult(
            removed=[
                name for name in old_columns_by_name if name not in new_column_names
            ]
        )

        columns: list[TableColumn] = []
        for col in new_columns:
            old_column = old_columns_by_name.pop(col["column_name"], None)
            if not old_column:
                results.added.append(col["column_name"])
                new_column = TableColumn(
                    column_name=col["column_name"],
                    type=col["type"],
                    table=model,
                )
                new_column.is_dttm = new_column.is_temporal
                if col.get("comment"):
                    new_column.description = col["comment"]
                db_engine_spec.alter_new_orm_column(new_column)
            else:
                new_column = old_column
                if new_column.type != col["type"]:
                    results.modified.append(col["column_name"])
                new_column.type = col["type"]
                new_column.expression = ""
                if col.get("comment"):
                    new_column.description = col["comment"]
            new_column.groupby = True
            new_column.filterable = True
            columns.append(new_column)
            if not any_date_col and new_column.is_temporal:
                any_date_col = col["column_name"]

        # add back calculated (virtual) columns
        columns.extend([col for col in old_columns if col.expression])
        # Reassigning the (already-loaded) collection lets the
        # ``all, delete-orphan`` cascade drop columns no longer present.
        model.columns = columns

        if not model.main_dttm_col:
            model.main_dttm_col = any_date_col
        model.add_missing_metrics(metrics)

        _apply_sqla_table_mutator(model)

        await self.session.flush()
        return results

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
