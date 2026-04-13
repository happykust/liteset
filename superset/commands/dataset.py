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
"""Dataset command classes — business logic for dataset CRUD and operations."""

from __future__ import annotations

import gzip
import io
import json
import logging
import re
from typing import Any, TYPE_CHECKING
from urllib import request as url_request

import pandas as pd
import yaml  # type: ignore[import-untyped]
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, String, Text

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
)
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.importexport.import_base import AsyncImportModelsCommand
from superset.tags.core import (
    add_implicit_tags_after_insert,
    delete_tagged_objects,
    sync_owner_tags_after_update,
)
from superset.utils import mask_uri_password

if TYPE_CHECKING:
    from sqlalchemy.sql.visitors import VisitableType

    from superset.db.daos.dataset import (
        AsyncDatasetColumnDAO,
        AsyncDatasetDAO,
        AsyncDatasetMetricDAO,
    )
    from superset.models.connectors import SqlaTable

logger = logging.getLogger(__name__)

EXPORT_VERSION = "1.0.0"
CHUNKSIZE = 512
VARCHAR = re.compile(r"VARCHAR\((\d+)\)", re.IGNORECASE)
JSON_KEYS = {"params", "template_params", "extra"}

# Regex to sanitize file names
_SAFE_FILENAME_RE = re.compile(r"[^\w\s\-.]")

# Column type mapping for CSV data loading
_TYPE_MAP: dict[str, Any] = {
    "BOOLEAN": Boolean(),
    "VARCHAR": String(255),
    "STRING": String(255),
    "TEXT": Text(),
    "BIGINT": BigInteger(),
    "FLOAT": Float(),
    "FLOAT64": Float(),
    "DOUBLE PRECISION": Float(),
    "DATE": Date(),
    "DATETIME": DateTime(),
    "TIMESTAMP WITHOUT TIME ZONE": DateTime(timezone=False),
    "TIMESTAMP WITH TIME ZONE": DateTime(timezone=True),
}


def _safe_filename(name: str) -> str:
    """Create a safe filename from a model name."""
    name = _SAFE_FILENAME_RE.sub("", name).strip()
    return name or "unnamed"


def _get_sqla_type(native_type: str) -> VisitableType:
    """Map a native column type string to a SQLAlchemy type."""
    if native_type.upper() in _TYPE_MAP:
        return _TYPE_MAP[native_type.upper()]
    if match := VARCHAR.match(native_type):
        size = int(match.group(1))
        return String(size)
    raise ValueError(f"Unknown type: {native_type}")


def _get_dtype(
    df: pd.DataFrame,
    dataset: SqlaTable,
) -> dict[str, VisitableType]:
    """Build a dtype mapping from dataset columns for DataFrame.to_sql()."""
    return {
        column.column_name: _get_sqla_type(column.type)
        for column in (getattr(dataset, "columns", None) or [])
        if getattr(column, "type", None) and column.column_name in df.keys()
    }


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

    async def validate(self) -> None:  # noqa: C901
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
            schema=self._data.get("schema"),
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
            schema = self._data.get("schema")
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
            except Exception:  # noqa: S110
                pass  # Skip check if has_table is not available
        # Check schema access if security manager is available
        if self._security_manager is not None:
            schema = self._data.get("schema")
            if schema:
                try:
                    await self._security_manager.raise_for_access(
                        database=self._database,
                        schema=schema,
                        user=self._user_id,
                    )
                except Exception as exc:
                    raise CommandInvalidError(
                        f"Access denied to schema '{schema}'"
                    ) from exc
        # Validate SQL access for virtual datasets
        sql = self._data.get("sql")
        if sql and self._security_manager is not None and self._database is not None:
            if hasattr(self._security_manager, "raise_for_access"):
                try:
                    await self._security_manager.raise_for_access(
                        database=self._database,
                        schema=self._data.get("schema"),
                        sql=sql,
                        user=self._user_id,
                    )
                except Exception as exc:
                    raise CommandInvalidError(
                        "Access denied: insufficient SQL access"
                    ) from exc

    async def run(self) -> "SqlaTable":
        from superset.models.connectors import SqlaTable

        # Resolve catalog: use provided value or fall back to database default
        catalog = self._data.get("catalog")
        if not catalog and self._database is not None:
            if hasattr(self._database, "get_default_catalog"):
                catalog = self._database.get_default_catalog()

        dataset = SqlaTable(
            table_name=self._data["table_name"],
            database_id=self._data["database"],
            schema=self._data.get("schema"),
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

        # Add implicit type: and owner: tags (async port of DatasetUpdater.after_insert)
        await self._dao.session.refresh(dataset, ["owners"])
        owner_ids = [o.id for o in dataset.owners] if hasattr(dataset, "owners") else []
        await add_implicit_tags_after_insert(
            self._dao.session, "dataset", dataset.id, owner_ids
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

    async def validate(self) -> None:  # noqa: C901
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
                schema=self._data.get("schema"),
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
                schema = self._data.get("schema") or getattr(
                    self._dataset, "schema", None
                )
                try:
                    await self._security_manager.raise_for_access(
                        database=database,
                        schema=schema,
                        sql=sql,
                        user=self._user_id,
                    )
                except Exception as exc:
                    raise CommandInvalidError(
                        "Access denied: insufficient SQL access"
                    ) from exc

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
            raise ObjectNotFoundError("Dataset", str(missing))
        if self._security_manager is not None:
            for dataset in self._datasets:
                await self._security_manager.raise_for_ownership(dataset, self._user_id)

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
            database_id=int(self._source.database_id),
            table_name=self._table_name,
            schema=getattr(self._source, "schema", None),
        )
        if not is_unique:
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
                )
                new_col.table_id = new_dataset.id
                self._dao.session.add(new_col)

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
                new_metric.table_id = new_dataset.id
                self._dao.session.add(new_metric)

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
        if not self._data.get("database_id"):
            raise CommandInvalidError("database_id is required")

    async def run(self) -> "SqlaTable":
        from superset.models.connectors import SqlaTable

        existing = await self._dao.find_one_or_none(
            table_name=self._data["table_name"],
            database_id=self._data["database_id"],
        )
        if existing:
            return existing
        dataset = SqlaTable(
            table_name=self._data["table_name"],
            database_id=self._data["database_id"],
            schema=self._data.get("schema"),
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

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:  # noqa: C901
        """Export a dataset with all columns, metrics, and related database.

        Port of superset_old/commands/dataset/export.py ExportDatasetsCommand.
        """
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")
        dataset = await self._dao.find_by_id(model_id)
        if not dataset:
            raise ObjectNotFoundError("Dataset", model_id)

        db = getattr(dataset, "database", None)
        files: list[tuple[str, str]] = []

        # -- Dataset YAML ---------------------------------------------------
        db_file_name = _safe_filename(db.database_name) if db else "unknown_database"
        ds_file_name = _safe_filename(getattr(dataset, "table_name", "unknown"))

        payload: dict[str, Any] = {
            "table_name": dataset.table_name,
            "main_dttm_col": getattr(dataset, "main_dttm_col", None),
            "description": getattr(dataset, "description", None),
            "default_endpoint": getattr(dataset, "default_endpoint", None),
            "offset": getattr(dataset, "offset", 0),
            "cache_timeout": getattr(dataset, "cache_timeout", None),
            "schema": getattr(dataset, "schema", None),
            "sql": getattr(dataset, "sql", None),
            "params": getattr(dataset, "params", None),
            "template_params": getattr(dataset, "template_params", None),
            "filter_select_enabled": getattr(dataset, "filter_select_enabled", True),
            "fetch_values_predicate": getattr(dataset, "fetch_values_predicate", None),
            "extra": getattr(dataset, "extra", None),
            "normalize_columns": getattr(dataset, "normalize_columns", False),
            "always_filter_main_dttm": getattr(
                dataset, "always_filter_main_dttm", False
            ),
            "uuid": (str(dataset.uuid) if getattr(dataset, "uuid", None) else None),
        }

        # Parse JSON string fields into dicts for export
        for key in JSON_KEYS:
            if payload.get(key) and isinstance(payload[key], str):
                try:
                    payload[key] = json.loads(payload[key])
                except (json.JSONDecodeError, TypeError):
                    logger.info("Unable to decode `%s` field: %s", key, payload[key])

        # Columns
        columns: list[dict[str, Any]] = []
        for col in getattr(dataset, "columns", []) or []:
            col_dict: dict[str, Any] = {
                "column_name": col.column_name,
                "verbose_name": getattr(col, "verbose_name", None),
                "is_dttm": getattr(col, "is_dttm", False),
                "is_active": getattr(col, "is_active", True),
                "type": getattr(col, "type", None),
                "advanced_data_type": getattr(col, "advanced_data_type", None),
                "groupby": getattr(col, "groupby", True),
                "filterable": getattr(col, "filterable", True),
                "expression": getattr(col, "expression", None),
                "description": getattr(col, "description", None),
                "python_date_format": getattr(col, "python_date_format", None),
                "extra": getattr(col, "extra", None),
                "uuid": (str(col.uuid) if getattr(col, "uuid", None) else None),
            }
            if col_dict.get("extra") and isinstance(col_dict["extra"], str):
                try:
                    col_dict["extra"] = json.loads(col_dict["extra"])
                except (json.JSONDecodeError, TypeError):
                    logger.info(
                        "Unable to decode `extra` field: %s",
                        col_dict["extra"],
                    )
            columns.append(col_dict)
        payload["columns"] = columns

        # Metrics
        metrics: list[dict[str, Any]] = []
        for m in getattr(dataset, "metrics", []) or []:
            m_dict: dict[str, Any] = {
                "metric_name": m.metric_name,
                "verbose_name": getattr(m, "verbose_name", None),
                "metric_type": getattr(m, "metric_type", None),
                "expression": m.expression,
                "description": getattr(m, "description", None),
                "d3format": getattr(m, "d3format", None),
                "currency": getattr(m, "currency", None),
                "extra": getattr(m, "extra", None),
                "warning_text": getattr(m, "warning_text", None),
                "uuid": (str(m.uuid) if getattr(m, "uuid", None) else None),
            }
            if m_dict.get("extra") and isinstance(m_dict["extra"], str):
                try:
                    m_dict["extra"] = json.loads(m_dict["extra"])
                except (json.JSONDecodeError, TypeError):
                    logger.info("Unable to decode `extra` field: %s", m_dict["extra"])
            metrics.append(m_dict)
        payload["metrics"] = metrics

        payload["version"] = EXPORT_VERSION
        payload["database_uuid"] = (
            str(db.uuid) if db and getattr(db, "uuid", None) else None
        )

        files.append(
            (
                f"datasets/{db_file_name}/{ds_file_name}.yaml",
                yaml.safe_dump(payload, sort_keys=False),
            )
        )

        # -- Related database YAML ------------------------------------------
        if db:
            db_payload: dict[str, Any] = {
                "database_name": db.database_name,
                "sqlalchemy_uri": mask_uri_password(getattr(db, "sqlalchemy_uri", "")),
                "cache_timeout": getattr(db, "cache_timeout", None),
                "expose_in_sqllab": getattr(db, "expose_in_sqllab", True),
                "allow_run_async": getattr(db, "allow_run_async", False),
                "allow_ctas": getattr(db, "allow_ctas", False),
                "allow_cvas": getattr(db, "allow_cvas", False),
                "allow_dml": getattr(db, "allow_dml", False),
                "allow_csv_upload": getattr(db, "allow_file_upload", False),
                "extra": getattr(db, "extra", "{}"),
                "uuid": (str(db.uuid) if getattr(db, "uuid", None) else None),
            }

            # Parse extra JSON
            if db_payload.get("extra") and isinstance(db_payload["extra"], str):
                try:
                    db_payload["extra"] = json.loads(db_payload["extra"])
                except (json.JSONDecodeError, TypeError):
                    logger.info(
                        "Unable to decode `extra` field: %s",
                        db_payload["extra"],
                    )

            # SSH tunnel with masked passwords
            if hasattr(self._dao, "get_database_by_id"):
                # Try to get SSH tunnel via the database DAO
                pass  # SSH tunnel lookup delegated below

            from superset.db.daos.database import AsyncSSHTunnelDAO

            ssh_dao = AsyncSSHTunnelDAO(self._dao.session)
            ssh_tunnel = await ssh_dao.get_by_database_id(db.id)
            if ssh_tunnel:
                ssh_payload: dict[str, Any] = {
                    "server_address": ssh_tunnel.server_address,
                    "server_port": ssh_tunnel.server_port,
                    "username": ssh_tunnel.username,
                    "password": getattr(ssh_tunnel, "password", None),
                    "private_key": getattr(ssh_tunnel, "private_key", None),
                    "private_key_password": getattr(
                        ssh_tunnel, "private_key_password", None
                    ),
                }
                # Mask passwords
                for key in (
                    "password",
                    "private_key",
                    "private_key_password",
                ):
                    if ssh_payload.get(key):
                        ssh_payload[key] = "XXXXXXXXXX"
                db_payload["ssh_tunnel"] = ssh_payload

            db_payload["version"] = EXPORT_VERSION

            files.append(
                (
                    f"databases/{db_file_name}.yaml",
                    yaml.safe_dump(db_payload, sort_keys=False),
                )
            )

        return files


class ImportDatasetsCommand(AsyncImportModelsCommand):
    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncDatasetDAO | None = None,
        sync_columns: bool = True,
        sync_metrics: bool = True,
        security_manager: Any | None = None,
        ignore_permissions: bool = False,
        force_data: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao
        self._sync_columns = sync_columns
        self._sync_metrics = sync_metrics
        self._security_manager = security_manager
        self._ignore_permissions = ignore_permissions
        self._force_data = force_data

    _IMPORT_ORDER = ("databases/", "datasets/")

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        for name, config in configs.items():
            if name.startswith("datasets/") and not config.get("table_name"):
                raise CommandInvalidError(f"Missing table_name in {name}")

    async def run(self) -> None:
        """Override to ensure dependency order: databases -> datasets."""
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        configs = self._configs

        def _sort_key(item: tuple[str, Any]) -> int:
            name = item[0]
            for idx, prefix in enumerate(self._IMPORT_ORDER):
                if name.startswith(prefix):
                    return idx
            return len(self._IMPORT_ORDER)

        for file_name, content in sorted(configs.items(), key=_sort_key):
            if file_name == "metadata.yaml":
                continue
            if isinstance(content, dict):
                content = self._apply_password(content)
            await self._import_single(file_name, content)

    async def _check_existing(self, uuid_val: str) -> bool:
        """Check if a dataset with this UUID already exists."""
        from uuid import UUID as _UUID

        if self._dao is None:
            return False
        result = await self._dao.find_one_or_none(uuid=_UUID(uuid_val))
        return result is not None

    async def _import_single(  # noqa: C901
        self,
        file_name: str,
        content: dict[str, Any],
    ) -> None:
        """Import a single file from the bundle.

        Handles both databases/ and datasets/ prefixed files.
        """
        if file_name.startswith("databases/"):
            await self._import_database(file_name, content)
            return
        if not file_name.startswith("datasets/"):
            return
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        await self._import_dataset(content)

    async def _import_database(  # noqa: C901
        self,
        file_name: str,
        content: dict[str, Any],
    ) -> None:
        """Import a dependent database from the bundle."""
        if self._dao is None:
            return

        from uuid import UUID as _UUID

        from superset.models.core import Database

        uuid_str = content.get("uuid")
        existing: Database | None = None
        if uuid_str:
            from sqlalchemy import select

            stmt = select(Database).where(Database.uuid == _UUID(uuid_str))
            result = await self._dao.session.execute(stmt)
            existing = result.scalars().one_or_none()

        if existing:
            if not self._overwrite:
                return  # skip
            # Update existing
            for key in (
                "database_name",
                "sqlalchemy_uri",
                "cache_timeout",
                "expose_in_sqllab",
                "allow_run_async",
                "allow_ctas",
                "allow_cvas",
                "allow_dml",
                "extra",
            ):
                if key in content:
                    value = content[key]
                    if key == "allow_csv_upload":
                        existing.allow_file_upload = value
                    else:
                        setattr(existing, key, value)
            await self._dao.session.flush()
        else:
            config = dict(content)
            if "allow_csv_upload" in config:
                config["allow_file_upload"] = config.pop("allow_csv_upload")
            config.pop("version", None)
            config.pop("ssh_tunnel", None)
            db = Database(
                database_name=config.get("database_name", ""),
                sqlalchemy_uri=config.get("sqlalchemy_uri", ""),
            )
            if uuid_str:
                db.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            for key in (
                "cache_timeout",
                "expose_in_sqllab",
                "allow_run_async",
                "allow_ctas",
                "allow_cvas",
                "allow_dml",
                "allow_file_upload",
                "extra",
            ):
                if key in config:
                    setattr(db, key, config[key])
            self._dao.session.add(db)
            await self._dao.session.flush()

    async def _import_dataset(  # noqa: C901
        self,
        content: dict[str, Any],
    ) -> None:
        """Import a single dataset — 1:1 port of import_dataset().

        Logic ported from superset_old/commands/dataset/importers/v1/utils.py:
        1. UUID-based dedup with MultipleResultsFound handling
        2. All dataset fields
        3. Recursive column and metric import with sync
        4. Data loading from CSV URI
        5. Owner management
        """
        assert self._dao is not None

        from uuid import UUID as _UUID

        from sqlalchemy import select
        from sqlalchemy.exc import MultipleResultsFound

        from superset.models.connectors import SqlaTable
        from superset.models.core import Database

        config = dict(content)  # shallow copy

        # --- Permission check ---
        can_write = self._ignore_permissions
        if not can_write and self._security_manager is not None:
            if hasattr(self._security_manager, "can_access"):
                can_write = await self._security_manager.can_access(
                    "can_write", "Dataset"
                )
            else:
                can_write = True
        elif self._security_manager is None:
            can_write = True

        # --- UUID-based dedup ---
        uuid_str = config.get("uuid")
        existing: SqlaTable | None = None
        if uuid_str:
            existing = await self._dao.find_one_or_none(uuid=_UUID(uuid_str))

        if existing:
            if not self._overwrite or not can_write:
                return  # skip
            config["id"] = existing.id
        elif not can_write:
            raise CommandInvalidError(
                "Dataset doesn't exist and user doesn't have permission "
                "to create datasets"
            )

        # --- Resolve database by UUID if database_id not present ---
        database_id = config.get("database_id")
        db_uuid = config.get("database_uuid")
        if not database_id and db_uuid:
            stmt = select(Database).where(Database.uuid == _UUID(db_uuid))
            result = await self._dao.session.execute(stmt)
            db = result.scalars().one_or_none()
            if db:
                database_id = db.id
                config["database_id"] = database_id
        if not database_id:
            raise CommandInvalidError(
                f"Cannot import dataset '{config.get('table_name', '')}': "
                "database_id is required "
                "(provide database_id or database_uuid in export)"
            )

        # --- Serialize JSON fields ---
        for key in JSON_KEYS:
            if config.get(key) is not None and isinstance(config[key], dict):
                try:
                    config[key] = json.dumps(config[key])
                except TypeError:
                    logger.info("Unable to encode `%s` field: %s", key, config[key])

        # Serialize extra fields in columns and metrics
        for key in ("metrics", "columns"):
            for attributes in config.get(key, []):
                if (
                    isinstance(attributes, dict)
                    and attributes.get("extra") is not None
                    and isinstance(attributes["extra"], dict)
                ):
                    try:
                        attributes["extra"] = json.dumps(attributes["extra"])
                    except TypeError:
                        logger.info(
                            "Unable to encode `extra` field: %s",
                            attributes["extra"],
                        )
                        attributes["extra"] = None

        # Should we delete columns and metrics not present in import?
        sync_columns = self._sync_columns and self._overwrite
        sync_metrics = self._sync_metrics and self._overwrite

        # Data URI for loading CSV data
        data_uri = config.get("data")

        # --- Remove non-model fields ---
        config.pop("version", None)
        config.pop("database_uuid", None)
        config.pop("data", None)

        # --- Extract columns and metrics for separate handling ---
        columns_config = config.pop("columns", []) or []
        metrics_config = config.pop("metrics", []) or []

        # --- Dataset attribute mapping ---
        dataset_attrs = {
            "table_name",
            "main_dttm_col",
            "description",
            "default_endpoint",
            "offset",
            "cache_timeout",
            "schema",
            "catalog",
            "sql",
            "params",
            "template_params",
            "filter_select_enabled",
            "fetch_values_predicate",
            "extra",
            "normalize_columns",
            "always_filter_main_dttm",
            "is_sqllab_view",
            "database_id",
            "perm",
            "schema_perm",
        }

        if existing:
            # Update existing dataset
            for key in dataset_attrs:
                if key in config:
                    setattr(existing, key, config[key])
            if uuid_str:
                existing.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            dataset = existing
        else:
            # Create new dataset
            filtered_attrs = {k: v for k, v in config.items() if k in dataset_attrs}
            try:
                dataset = SqlaTable(**filtered_attrs)
            except MultipleResultsFound:
                # Legacy edge case: dataset without schema was later fixed
                # to have a default schema, causing conflicts.
                if uuid_str:
                    dataset = await self._dao.find_one_or_none(uuid=_UUID(uuid_str))
                    if dataset:
                        return
                raise
            if uuid_str:
                dataset.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            self._dao.session.add(dataset)

        await self._dao.session.flush()

        # --- Import columns ---
        await self._import_columns(dataset, columns_config, sync=sync_columns)

        # --- Import metrics ---
        await self._import_metrics(dataset, metrics_config, sync=sync_metrics)

        await self._dao.session.flush()

        # --- Load data from URI if needed ---
        if data_uri and self._force_data:
            try:
                await self._load_data(data_uri, dataset)
            except Exception:
                logger.warning(
                    "Failed to load data from %s for dataset %s",
                    data_uri,
                    getattr(dataset, "table_name", ""),
                    exc_info=True,
                )

        # --- Owner management ---
        # Add current user as owner if not already
        if self._security_manager is not None and hasattr(
            self._security_manager, "get_current_user"
        ):
            user = self._security_manager.get_current_user()
            if user and hasattr(dataset, "owners"):
                await self._dao.session.refresh(dataset, ["owners"])  # type: ignore[union-attr]
                if user not in dataset.owners:
                    dataset.owners.append(user)

    async def _import_columns(  # noqa: C901
        self,
        dataset: SqlaTable,
        columns_config: list[dict[str, Any]],
        sync: bool = False,
    ) -> None:
        """Import columns into a dataset, optionally syncing (deleting absent ones)."""
        from uuid import UUID as _UUID

        from sqlalchemy import delete, select

        from superset.models.connectors import TableColumn

        if not columns_config and not sync:
            return

        # Get existing columns
        stmt = select(TableColumn).where(TableColumn.table_id == dataset.id)
        result = await self._dao.session.execute(stmt)  # type: ignore[union-attr]
        existing_by_uuid: dict[str, TableColumn] = {}
        existing_by_name: dict[str, TableColumn] = {}
        existing_ids: set[int] = set()
        for col in result.scalars().all():
            if getattr(col, "uuid", None):
                existing_by_uuid[str(col.uuid)] = col
            existing_by_name[col.column_name] = col
            existing_ids.add(col.id)

        seen_ids: set[int] = set()
        col_attrs = {
            "column_name",
            "verbose_name",
            "is_dttm",
            "is_active",
            "type",
            "advanced_data_type",
            "groupby",
            "filterable",
            "expression",
            "description",
            "python_date_format",
            "extra",
        }

        for col_data in columns_config:
            if not isinstance(col_data, dict):
                continue
            col_uuid = col_data.get("uuid")
            col_name = col_data.get("column_name", "")

            # Try to find existing column by UUID, then by name
            existing_col = None
            if col_uuid:
                existing_col = existing_by_uuid.get(col_uuid)
            if existing_col is None:
                existing_col = existing_by_name.get(col_name)

            if existing_col:
                # Update existing column
                for key in col_attrs:
                    if key in col_data:
                        setattr(existing_col, key, col_data[key])
                if col_uuid:
                    existing_col.uuid = _UUID(col_uuid)  # type: ignore[assignment]
                seen_ids.add(existing_col.id)
            else:
                # Create new column
                filtered = {k: v for k, v in col_data.items() if k in col_attrs}
                filtered["table_id"] = dataset.id
                new_col = TableColumn(**filtered)
                if col_uuid:
                    new_col.uuid = _UUID(col_uuid)  # type: ignore[assignment]
                self._dao.session.add(new_col)  # type: ignore[union-attr]

        # Sync: delete columns not present in the import
        if sync:
            ids_to_delete = existing_ids - seen_ids
            if ids_to_delete:
                del_stmt = delete(TableColumn).where(TableColumn.id.in_(ids_to_delete))
                await self._dao.session.execute(del_stmt)  # type: ignore[union-attr]

    async def _import_metrics(  # noqa: C901
        self,
        dataset: SqlaTable,
        metrics_config: list[dict[str, Any]],
        sync: bool = False,
    ) -> None:
        """Import metrics into a dataset, optionally syncing."""
        from uuid import UUID as _UUID

        from sqlalchemy import delete, select

        from superset.models.connectors import SqlMetric

        if not metrics_config and not sync:
            return

        # Get existing metrics
        stmt = select(SqlMetric).where(SqlMetric.table_id == dataset.id)
        result = await self._dao.session.execute(stmt)  # type: ignore[union-attr]
        existing_by_uuid: dict[str, SqlMetric] = {}
        existing_by_name: dict[str, SqlMetric] = {}
        existing_ids: set[int] = set()
        for m in result.scalars().all():
            if getattr(m, "uuid", None):
                existing_by_uuid[str(m.uuid)] = m
            existing_by_name[m.metric_name] = m
            existing_ids.add(m.id)

        seen_ids: set[int] = set()
        metric_attrs = {
            "metric_name",
            "verbose_name",
            "metric_type",
            "expression",
            "description",
            "d3format",
            "currency",
            "extra",
            "warning_text",
        }

        for m_data in metrics_config:
            if not isinstance(m_data, dict):
                continue
            m_uuid = m_data.get("uuid")
            m_name = m_data.get("metric_name", "")

            existing_m = None
            if m_uuid:
                existing_m = existing_by_uuid.get(m_uuid)
            if existing_m is None:
                existing_m = existing_by_name.get(m_name)

            if existing_m:
                for key in metric_attrs:
                    if key in m_data:
                        setattr(existing_m, key, m_data[key])
                if m_uuid:
                    existing_m.uuid = _UUID(m_uuid)  # type: ignore[assignment]
                seen_ids.add(existing_m.id)
            else:
                filtered = {k: v for k, v in m_data.items() if k in metric_attrs}
                filtered["table_id"] = dataset.id
                new_metric = SqlMetric(**filtered)
                if m_uuid:
                    new_metric.uuid = _UUID(m_uuid)  # type: ignore[assignment]
                self._dao.session.add(new_metric)  # type: ignore[union-attr]

        if sync:
            ids_to_delete = existing_ids - seen_ids
            if ids_to_delete:
                del_stmt = delete(SqlMetric).where(SqlMetric.id.in_(ids_to_delete))
                await self._dao.session.execute(del_stmt)  # type: ignore[union-attr]

    async def _load_data(  # noqa: C901
        self,
        data_uri: str,
        dataset: SqlaTable,
    ) -> None:
        """Load data from a URI into the dataset's table.

        Port of superset_old/commands/dataset/importers/v1/utils.py load_data().
        """
        import asyncio

        logger.info("Downloading data from %s", data_uri)
        data = await asyncio.to_thread(
            url_request.urlopen,
            data_uri,  # noqa: S310
        )
        if data_uri.endswith(".gz"):
            data = gzip.open(data)
        df = await asyncio.to_thread(pd.read_csv, data, encoding="utf-8")
        dtype = _get_dtype(df, dataset)

        # Convert temporal columns
        for column_name, sqla_type in dtype.items():
            if isinstance(sqla_type, (Date, DateTime)):
                df[column_name] = pd.to_datetime(df[column_name])

        # Load data using the database engine
        database = getattr(dataset, "database", None)
        if database is None:
            db_obj = await self._dao.get_database_by_id(dataset.database_id)
            database = db_obj

        if database is None:
            logger.warning(
                "Cannot load data: database not found for dataset %s",
                dataset.table_name,
            )
            return

        table_name = dataset.table_name
        schema = getattr(dataset, "schema", None)

        # Use sync engine via to_thread for data loading
        from sqlalchemy import create_engine

        raw_uri = getattr(database, "sqlalchemy_uri", "")
        if not raw_uri:
            logger.warning("Cannot load data: no sqlalchemy_uri on database")
            return

        # Convert async driver URI to sync for pandas to_sql
        uri = raw_uri
        if "+asyncpg" in uri:
            uri = uri.replace("+asyncpg", "+psycopg2")
        elif "+aiomysql" in uri or "+asyncmy" in uri:
            uri = uri.replace("+aiomysql", "+pymysql").replace("+asyncmy", "+pymysql")
        elif "+aiosqlite" in uri:
            uri = uri.replace("+aiosqlite", "")

        def _load_sync() -> None:
            engine = create_engine(uri)
            df.to_sql(
                table_name,
                con=engine,
                schema=schema,
                if_exists="replace",
                chunksize=CHUNKSIZE,
                dtype=dtype,
                index=False,
                method="multi",
            )
            engine.dispose()

        await asyncio.to_thread(_load_sync)


class WarmUpDatasetCacheCommand(AsyncBaseCommand[list[dict[str, Any]]]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        db_name: str,
        table_name: str,
        dashboard_id: int | None = None,
        extra_filters: str | None = None,
    ) -> None:
        self._dao = dao
        self._db_name = db_name
        self._table_name = table_name
        self._dashboard_id = dashboard_id
        self._extra_filters = extra_filters

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
