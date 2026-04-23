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
"""Async port of ``superset_old/commands/dataset/importers/v1/__init__.py``."""

from __future__ import annotations

import gzip
import io
import json
import logging
import re
from typing import Any, TYPE_CHECKING
from urllib import request as url_request

import pandas as pd
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, String, Text

from superset.exceptions import CommandInvalidError
from superset.importexport.import_base import AsyncImportModelsCommand

if TYPE_CHECKING:
    from sqlalchemy.sql.visitors import VisitableType

    from superset.db.daos.dataset import AsyncDatasetDAO
    from superset.models.connectors import SqlaTable

logger = logging.getLogger(__name__)

CHUNKSIZE = 512
VARCHAR = re.compile(r"VARCHAR\((\d+)\)", re.IGNORECASE)
JSON_KEYS = {"params", "template_params", "extra"}

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


def _get_sqla_type(native_type: str) -> "VisitableType":
    """Map a native column type string to a SQLAlchemy type."""
    if native_type.upper() in _TYPE_MAP:
        return _TYPE_MAP[native_type.upper()]
    if match := VARCHAR.match(native_type):
        size = int(match.group(1))
        return String(size)
    raise ValueError(f"Unknown type: {native_type}")


def _get_dtype(
    df: pd.DataFrame,
    dataset: "SqlaTable",
) -> "dict[str, VisitableType]":
    """Build a dtype mapping from dataset columns for DataFrame.to_sql()."""
    return {
        column.column_name: _get_sqla_type(column.type)
        for column in (getattr(dataset, "columns", None) or [])
        if getattr(column, "type", None) and column.column_name in df.keys()
    }


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
        dataset: "SqlaTable",
        columns_config: list[dict[str, Any]],
        sync: bool = False,
    ) -> None:
        """Import columns into a dataset, optionally syncing (deleting absent ones)."""
        from uuid import UUID as _UUID

        from superset.models.connectors import TableColumn

        if not columns_config and not sync:
            return

        # Refresh relationship to get current DB state
        await self._dao.session.refresh(dataset, ["columns"])  # type: ignore[union-attr]
        existing_by_uuid: dict[str, TableColumn] = {}
        existing_by_name: dict[str, TableColumn] = {}
        existing_ids: set[int] = set()
        for col in dataset.columns:
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
                dataset.columns.append(new_col)

        # Sync: delete columns not present in the import
        if sync:
            ids_to_delete = existing_ids - seen_ids
            for cid in ids_to_delete:
                col_obj = next((c for c in dataset.columns if c.id == cid), None)
                if col_obj:
                    dataset.columns.remove(col_obj)
                    await self._dao.session.delete(col_obj)  # type: ignore[union-attr]

    async def _import_metrics(  # noqa: C901
        self,
        dataset: "SqlaTable",
        metrics_config: list[dict[str, Any]],
        sync: bool = False,
    ) -> None:
        """Import metrics into a dataset, optionally syncing."""
        from uuid import UUID as _UUID

        from superset.models.connectors import SqlMetric

        if not metrics_config and not sync:
            return

        # Refresh relationship to get current DB state
        await self._dao.session.refresh(dataset, ["metrics"])  # type: ignore[union-attr]
        existing_by_uuid: dict[str, SqlMetric] = {}
        existing_by_name: dict[str, SqlMetric] = {}
        existing_ids: set[int] = set()
        for m in dataset.metrics:
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
                dataset.metrics.append(new_metric)

        if sync:
            ids_to_delete = existing_ids - seen_ids
            for mid in ids_to_delete:
                m_obj = next((m for m in dataset.metrics if m.id == mid), None)
                if m_obj:
                    dataset.metrics.remove(m_obj)
                    await self._dao.session.delete(m_obj)  # type: ignore[union-attr]

    async def _load_data(  # noqa: C901
        self,
        data_uri: str,
        dataset: "SqlaTable",
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
