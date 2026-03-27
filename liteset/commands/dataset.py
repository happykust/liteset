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
"""Dataset command classes — business logic for dataset CRUD and operations."""

from __future__ import annotations

import io
from typing import Any, TYPE_CHECKING

import yaml  # type: ignore[import-untyped]

from liteset.commands.base import AsyncBaseCommand
from liteset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
)
from liteset.importexport.export_base import AsyncExportModelsCommand
from liteset.importexport.import_base import AsyncImportModelsCommand
from liteset.utils import mask_uri_password

if TYPE_CHECKING:
    from liteset.db.daos.dataset import (
        AsyncDatasetColumnDAO,
        AsyncDatasetDAO,
        AsyncDatasetMetricDAO,
    )
    from liteset.models.connectors import SqlaTable


class CreateDatasetCommand(AsyncBaseCommand["SqlaTable"]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._data.get("table_name"):
            raise CommandInvalidError("table_name is required")
        if not self._data.get("database"):
            raise CommandInvalidError("database is required")
        self._database = await self._dao.get_database_by_id(self._data["database"])
        if not self._database:
            raise CommandInvalidError("Database not found")
        is_unique = await self._dao.validate_uniqueness(
            database_id=self._data["database"],
            table_name=self._data["table_name"],
            schema=self._data.get("schema_name"),
        )
        if not is_unique:
            raise CommandInvalidError(
                "Dataset with this table_name/schema/database already exists"
            )
        # Validate table exists in the database (for physical datasets)
        sql = self._data.get("sql")
        if not sql and hasattr(self._database, "has_table"):
            import asyncio

            table_name = self._data["table_name"]
            schema = self._data.get("schema_name")
            try:
                exists = await asyncio.to_thread(
                    self._database.has_table, table_name, schema=schema
                )
                if not exists:
                    raise CommandInvalidError(
                        f"Table '{table_name}' does not exist in database"
                    )
            except CommandInvalidError:
                raise
            except Exception:
                pass  # Skip check if has_table is not available
        # Check schema access if security manager is available
        if self._security_manager is not None:
            schema = self._data.get("schema_name")
            if schema:
                try:
                    await self._security_manager.raise_for_access(
                        database=self._database,
                        schema=schema,
                        user=self._user_id,
                    )
                except Exception:
                    raise CommandInvalidError(f"Access denied to schema '{schema}'")
        # Validate SQL access for virtual datasets
        sql = self._data.get("sql")
        if sql and self._security_manager is not None and self._database is not None:
            if hasattr(self._security_manager, "raise_for_access"):
                try:
                    await self._security_manager.raise_for_access(
                        database=self._database,
                        schema=self._data.get("schema_name"),
                        sql=sql,
                        user=self._user_id,
                    )
                except Exception:
                    raise CommandInvalidError("Access denied: insufficient SQL access")

    async def run(self) -> "SqlaTable":
        from liteset.models.connectors import SqlaTable

        # Resolve catalog: use provided value or fall back to database default
        catalog = self._data.get("catalog")
        if not catalog and self._database is not None:
            if hasattr(self._database, "get_default_catalog"):
                catalog = self._database.get_default_catalog()

        dataset = SqlaTable(
            table_name=self._data["table_name"],
            database_id=self._data["database"],
            schema=self._data.get("schema_name"),
            sql=self._data.get("sql"),
            **({"catalog": catalog} if catalog else {}),
            is_managed_externally=self._data.get("is_managed_externally", False),
            external_url=self._data.get("external_url"),
            normalize_columns=self._data.get("normalize_columns", False),
            always_filter_main_dttm=self._data.get("always_filter_main_dttm", False),
        )
        if self._user_id is not None:
            dataset.created_by_fk = self._user_id
            dataset.changed_by_fk = self._user_id
        self._dao.session.add(dataset)
        await self._dao.session.flush()

        import asyncio

        if hasattr(dataset, "fetch_metadata"):
            try:
                await asyncio.to_thread(dataset.fetch_metadata)
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "fetch_metadata failed for new dataset", exc_info=True
                )

        return dataset


class UpdateDatasetCommand(AsyncBaseCommand["SqlaTable"]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        dataset_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dataset_id = dataset_id
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._dataset: Any | None = None

    async def validate(self) -> None:
        self._dataset = await self._dao.find_by_id(self._dataset_id)
        if not self._dataset:
            raise ObjectNotFoundError("Dataset", self._dataset_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dataset, self._user_id
            )
        # Validate duplicate column names
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

        # Validate duplicate metric names
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

        table_name = self._data.get("table_name")
        if table_name:
            database_id = self._data.get("database_id") or self._dataset.database_id
            is_unique = await self._dao.validate_uniqueness(
                database_id=int(database_id),
                table_name=table_name,
                schema=self._data.get("schema_name"),
                dataset_id=self._dataset_id,
            )
            if not is_unique:
                raise CommandInvalidError(
                    "Dataset with this table_name/schema/database already exists"
                )

    async def run(self) -> "SqlaTable":
        assert self._dataset is not None

        # Validate SQL access when sql is being changed
        sql = self._data.get("sql")
        if sql is not None and self._security_manager is not None:
            database = getattr(self._dataset, "database", None)
            if database and hasattr(self._security_manager, "raise_for_access"):
                schema = self._data.get("schema_name") or getattr(
                    self._dataset, "schema", None
                )
                try:
                    await self._security_manager.raise_for_access(
                        database=database,
                        schema=schema,
                        sql=sql,
                        user=self._user_id,
                    )
                except Exception:
                    raise CommandInvalidError("Access denied: insufficient SQL access")

        # Columns/metrics are handled by DAO.update() special logic
        data = dict(self._data)
        columns = data.pop("columns", None)
        metrics = data.pop("metrics", None)
        update_attrs: dict[str, Any] = {}
        if columns is not None:
            update_attrs["columns"] = [
                dict(c) if not isinstance(c, dict) else c for c in columns
            ]
        if metrics is not None:
            update_attrs["metrics"] = [
                dict(m) if not isinstance(m, dict) else m for m in metrics
            ]
        for key, value in data.items():
            if hasattr(self._dataset, key):
                update_attrs[key] = value
        if self._user_id is not None:
            update_attrs["changed_by_fk"] = self._user_id
        await self._dao.update(self._dataset, update_attrs)
        await self._dao.session.flush()
        return self._dataset


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

    async def validate(self) -> None:
        self._dataset = await self._dao.find_by_id(self._dataset_id)
        if not self._dataset:
            raise ObjectNotFoundError("Dataset", self._dataset_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dataset, self._user_id
            )

    async def run(self) -> None:
        assert self._dataset is not None
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
        found_ids = {d.id for d in self._datasets}
        missing = set(self._dataset_ids) - found_ids
        if missing:
            raise ObjectNotFoundError("Dataset", str(missing))
        if self._security_manager is not None:
            for dataset in self._datasets:
                await self._security_manager.raise_for_ownership(
                    dataset, self._user_id
                )

    async def run(self) -> None:
        for dataset in self._datasets:
            await self._dao.session.delete(dataset)
        await self._dao.session.flush()


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

        # Check that the new name doesn't already exist
        is_unique = await self._dao.validate_uniqueness(
            database_id=self._source.database_id,
            table_name=self._table_name,
            schema=getattr(self._source, "schema", None),
        )
        if not is_unique:
            raise CommandInvalidError(
                f"Dataset with name '{self._table_name}' already exists"
            )

    async def run(self) -> "SqlaTable":
        from liteset.models.connectors import SqlaTable, SqlMetric, TableColumn

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
        self._dao.session.add(new_dataset)
        await self._dao.session.flush()

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
                )
                new_dataset.metrics.append(new_metric)

        # Copy additional fields
        new_dataset.template_params = getattr(self._source, "template_params", None)
        new_dataset.normalize_columns = getattr(
            self._source, "normalize_columns", False
        )
        new_dataset.always_filter_main_dttm = getattr(
            self._source, "always_filter_main_dttm", False
        )

        await self._dao.session.flush()
        return new_dataset


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
        import asyncio

        if hasattr(self._dataset, "fetch_metadata"):
            await asyncio.to_thread(self._dataset.fetch_metadata)
        return self._dataset


class GetOrCreateDatasetCommand(AsyncBaseCommand["SqlaTable"]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id

    async def validate(self) -> None:
        if not self._data.get("table_name"):
            raise CommandInvalidError("table_name is required")
        if not self._data.get("database"):
            raise CommandInvalidError("database is required")

    async def run(self) -> "SqlaTable":
        from liteset.models.connectors import SqlaTable

        existing = await self._dao.find_one_or_none(
            table_name=self._data["table_name"],
            database_id=self._data["database"],
        )
        if existing:
            return existing
        dataset = SqlaTable(
            table_name=self._data["table_name"],
            database_id=self._data["database"],
            schema=self._data.get("schema_name"),
            template_params=self._data.get("template_params"),
            normalize_columns=self._data.get("normalize_columns", False),
            always_filter_main_dttm=self._data.get("always_filter_main_dttm", False),
        )
        if self._user_id is not None:
            dataset.created_by_fk = self._user_id
            dataset.changed_by_fk = self._user_id
        self._dao.session.add(dataset)
        await self._dao.session.flush()
        return dataset


class ExportDatasetsCommand(AsyncExportModelsCommand):
    _resource_type = "SqlaTable"

    def __init__(
        self, model_ids: list[int], dao: AsyncDatasetDAO | None = None
    ) -> None:
        super().__init__(model_ids)
        self._dao = dao

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")
        dataset = await self._dao.find_by_id(model_id)
        if not dataset:
            raise ObjectNotFoundError("Dataset", model_id)

        db = getattr(dataset, "database", None)
        dataset_data = {
            "table_name": dataset.table_name,
            "schema": getattr(dataset, "schema", None),
            "sql": getattr(dataset, "sql", None),
            "description": getattr(dataset, "description", None),
            "cache_timeout": dataset.cache_timeout,
            "uuid": str(dataset.uuid) if dataset.uuid else None,
            "database_uuid": str(db.uuid) if db and getattr(db, "uuid", None) else None,
        }
        files: list[tuple[str, str]] = [
            (
                f"datasets/{dataset.table_name}.yaml",
                yaml.safe_dump(dataset_data, sort_keys=False),
            ),
        ]
        # Bundle database YAML
        if db:
            db_data = {
                "database_name": db.database_name,
                "sqlalchemy_uri": mask_uri_password(db.sqlalchemy_uri),
                "uuid": str(db.uuid) if getattr(db, "uuid", None) else None,
            }
            files.append(
                (
                    f"databases/{db.database_name}.yaml",
                    yaml.safe_dump(db_data, sort_keys=False),
                )
            )
        return files


class ImportDatasetsCommand(AsyncImportModelsCommand):
    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncDatasetDAO | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        for name, config in configs.items():
            if name.startswith("datasets/") and not config.get("table_name"):
                raise CommandInvalidError(f"Missing table_name in {name}")

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        if not file_name.startswith("datasets/"):
            return
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        database_id = content.get("database_id")
        db_uuid = content.get("database_uuid")
        if not database_id and db_uuid and self._dao is not None:
            if hasattr(self._dao, "get_database_by_uuid"):
                database = await self._dao.get_database_by_uuid(db_uuid)
                if database:
                    database_id = database.id
        if not database_id:
            raise CommandInvalidError(
                f"Cannot import dataset '{content.get('table_name', '')}': "
                "database_id is required (provide database_id or database_uuid in export)"
            )

        from liteset.models.connectors import SqlaTable

        dataset = SqlaTable(
            table_name=content.get("table_name", ""),
            schema=content.get("schema"),
            sql=content.get("sql"),
            database_id=database_id,
        )
        self._dao.session.add(dataset)
        await self._dao.session.flush()


class WarmUpDatasetCacheCommand(AsyncBaseCommand[list[dict[str, Any]]]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        db_name: str,
        table_name: str,
        dashboard_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._db_name = db_name
        self._table_name = table_name
        self._dashboard_id = dashboard_id

    async def validate(self) -> None:
        if not self._db_name:
            raise CommandInvalidError("db_name is required")
        if not self._table_name:
            raise CommandInvalidError("table_name is required")

    async def run(self) -> list[dict[str, Any]]:
        return [
            {
                "db_name": self._db_name,
                "table_name": self._table_name,
                "status": "success",
            }
        ]


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
        dataset = await self._dataset_dao.find_by_id(self._dataset_id)
        if not dataset:
            raise ObjectNotFoundError("Dataset", self._dataset_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(dataset, self._user_id)
        self._column = await self._column_dao.find_by_dataset_and_id(
            self._dataset_id, self._column_id
        )
        if not self._column:
            raise ObjectNotFoundError("DatasetColumn", self._column_id)

    async def run(self) -> None:
        assert self._column is not None
        await self._column_dao.session.delete(self._column)
        await self._column_dao.session.flush()


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
        dataset = await self._dataset_dao.find_by_id(self._dataset_id)
        if not dataset:
            raise ObjectNotFoundError("Dataset", self._dataset_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(dataset, self._user_id)
        self._metric = await self._metric_dao.find_by_dataset_and_id(
            self._dataset_id, self._metric_id
        )
        if not self._metric:
            raise ObjectNotFoundError("DatasetMetric", self._metric_id)

    async def run(self) -> None:
        assert self._metric is not None
        await self._metric_dao.session.delete(self._metric)
        await self._metric_dao.session.flush()
