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

from superset.commands.dataset.exceptions import DatasetForbiddenDataURI
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
    # 1:1 with upstream metadata-type validation (``SqlaTable``).
    _expected_type = "SqlaTable"

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
                content = self._apply_password(content, file_name)
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

    async def _import_database(
        self,
        file_name: str,
        content: dict[str, Any],
    ) -> None:
        """Import a dependent database from the bundle.

        Delegates to the shared full ``_import_database`` port (the same
        function the chart/assets importers use) — 1:1 with upstream where
        the dataset importer calls the common
        ``commands.database.importers.v1.utils.import_database``.  That
        covers everything the previous inline copy dropped: ``extra``
        JSON-serialisation, the ``PREVENT_UNSAFE_DB_CONNECTIONS`` URI check,
        ``set_sqlalchemy_uri`` password masking, SSH-tunnel rows and
        catalog/schema ``add_permissions``.  ``overwrite=False`` is
        hardcoded upstream (``import_database(config, overwrite=False)``) —
        dependency databases are never overwritten on dataset import.
        """
        if self._dao is None:
            return

        from superset.commands.chart.importers.v1.utils import (
            _import_database as _shared_import_database,
        )

        await _shared_import_database(
            self._dao.session,
            dict(content),
            overwrite=False,
            # Without a security manager the permission gate lives on the
            # controller guard (can_write Dataset) — don't deny creation.
            ignore_permissions=(
                self._ignore_permissions or self._security_manager is None
            ),
            security_manager=self._security_manager,
        )

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

        from superset.models.connectors import SqlaTable
        from superset.models.core import Database

        config = dict(content)  # shallow copy

        from superset.utils.core import get_current_user

        user = get_current_user()

        # --- Permission check ---
        # ``AsyncSecurityManager.can_access`` takes the user explicitly
        # (keyword-only) — the upstream manager reads the request-scoped
        # current user inside ``can_access`` instead. No user in context →
        # deny, like upstream.
        can_write = self._ignore_permissions
        if not can_write and self._security_manager is not None:
            if hasattr(self._security_manager, "can_access"):
                can_write = user is not None and (
                    await self._security_manager.can_access(
                        "can_write", "Dataset", user=user
                    )
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

        # 1:1 port of import_dataset(): the importing user must own the
        # existing dataset (or be an admin) before they may overwrite it.

        if existing:
            if self._overwrite and can_write and user:
                await self._dao.session.refresh(existing, ["owners"])  # type: ignore[union-attr]
                is_admin = False
                if self._security_manager is not None and hasattr(
                    self._security_manager, "is_admin"
                ):
                    is_admin = self._security_manager.is_admin(user)
                if user not in existing.owners and not is_admin:
                    raise CommandInvalidError(
                        "A dataset already exists and user doesn't "
                        "have permissions to overwrite it"
                    )
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
            # extra_import_fields upstream (connectors/sqla/models.py:212) —
            # R11-11.
            "is_managed_externally",
            "external_url",
            # export_fields includes folders (models.py:1171) — R11-12.
            "folders",
        }

        collision = False
        if existing:
            # Historical two-row guard — 1:1 with upstream's
            # ``except MultipleResultsFound`` fallback (utils.py:161-170):
            # datasets were once imported without a schema (``db.NULL.tbl``)
            # and later fixed to the default schema (``db.public.tbl``); a
            # user-created row may already occupy the incoming name. If
            # ANOTHER row (different id) holds the incoming (database_id,
            # catalog, schema, table_name), applying the update would violate
            # ``uq_tables_database_catalog_schema_table`` (IntegrityError →
            # 500). Upstream returned the UUID-matched row unmodified — do
            # the same: keep ``existing`` as-is and skip attrs/columns/
            # metrics, but still flow into the data-loading step below.
            conflict_id = (
                (
                    await self._dao.session.execute(
                        select(SqlaTable.id).where(
                            SqlaTable.database_id == config.get("database_id"),
                            SqlaTable.catalog == config.get("catalog"),
                            SqlaTable.schema == config.get("schema"),
                            SqlaTable.table_name == config.get("table_name"),
                            SqlaTable.id != existing.id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            collision = conflict_id is not None
            if not collision:
                # Update existing dataset
                for key in dataset_attrs:
                    if key in config:
                        setattr(existing, key, config[key])
                if uuid_str:
                    existing.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            dataset = existing
        else:
            # Create new dataset. NOTE: upstream wrapped this in
            # ``except MultipleResultsFound`` because it called
            # ``SqlaTable.import_from_dict`` (an ORM dedup query that can raise
            # it); the async port does its own UUID dedup above and constructs
            # the model directly, which cannot raise MultipleResultsFound — so
            # the handler is omitted rather than kept as dead code.
            filtered_attrs = {k: v for k, v in config.items() if k in dataset_attrs}
            dataset = SqlaTable(**filtered_attrs)
            if uuid_str:
                dataset.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            self._dao.session.add(dataset)

        if not collision:
            await self._dao.session.flush()

            # --- Import columns ---
            await self._import_columns(dataset, columns_config, sync=sync_columns)

            # --- Import metrics ---
            await self._import_metrics(dataset, metrics_config, sync=sync_metrics)

            await self._dao.session.flush()

        # --- Load data from URI if needed ---
        # 1:1 port of import_dataset(): data is loaded when a ``data`` URI is
        # present AND either the target table does not yet exist OR the caller
        # forced a reload (``force_data``).
        if data_uri:
            try:
                table_exists = await self._table_exists(dataset)
            except Exception:  # noqa: BLE001
                # MySQL doesn't play nice with GSheets table names
                logger.warning(
                    "Couldn't check if table %s exists, assuming it does",
                    getattr(dataset, "table_name", ""),
                )
                table_exists = True

            if not table_exists or self._force_data:
                if collision:
                    # The collision branch skips ``_import_columns`` (which
                    # refreshes ``columns``), so ``_get_dtype`` would trip a
                    # sync lazy-load (MissingGreenlet). Load the relationship
                    # first — upstream's sync session lazy-loaded it on
                    # demand here.
                    await self._dao.session.refresh(dataset, ["columns"])  # type: ignore[union-attr]
                # NO try/except — upstream ``load_data`` failures (incl.
                # DatasetForbiddenDataURI and download/engine errors)
                # propagate and fail the whole import (R11-10: a swallow-all
                # used to turn them into a silently "successful" import
                # without data).
                await self._load_data(data_uri, dataset)

        # --- Owner management ---
        # Add the importing user as owner — 1:1 with upstream utils.py:189
        # ``if (user := get_user()) and user not in dataset.owners: ...``.
        # Resolve via the request-scoped ContextVar (the async equivalent of
        # the request-scoped current user); the previous gate on
        # ``hasattr(security_manager, "get_current_user")`` was ALWAYS False
        # (AsyncSecurityManager has no such method), so the owner was never set.
        from superset.utils.core import get_current_user as _get_current_user

        owner = _get_current_user()
        if owner is not None:
            # Refresh ``owners`` FIRST — reading the relationship (even via
            # ``hasattr``) before it's loaded triggers a sync lazy-load on the
            # async session → MissingGreenlet. The dataset was flushed above.
            await self._dao.session.refresh(dataset, ["owners"])  # type: ignore[union-attr]
            if owner not in dataset.owners:
                dataset.owners.append(owner)

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

    @staticmethod
    def _normalize_example_data_url(data_uri: str) -> str:
        """Convert ``examples://`` URLs to the configured CDN URL.

        Port of ``normalize_example_data_url`` usage in the original
        ``load_data`` — leaves non-example URLs untouched.
        """
        try:
            from superset.examples.helpers import normalize_example_data_url
        except ImportError:
            return data_uri
        return normalize_example_data_url(data_uri)

    @staticmethod
    def _validate_data_uri(data_uri: str) -> None:
        """Validate ``data_uri`` against ``DATASET_IMPORT_ALLOWED_DATA_URLS``.

        1:1 port of ``superset_old/commands/dataset/importers/v1/utils.py``
        ``validate_data_uri``: any allow-list regex matching the URI passes;
        otherwise the original raised ``DatasetForbiddenDataURI`` (HTTP 500).
        """
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            allowed_urls: list[str] = getattr(
                settings, "dataset_import_allowed_data_urls", [r".*"]
            )
        except Exception:  # noqa: BLE001
            allowed_urls = [r".*"]

        for allowed_url in allowed_urls:
            try:
                if re.match(allowed_url, data_uri):
                    return
            except re.error:
                logger.exception(
                    "Invalid regular expression on DATASET_IMPORT_ALLOWED_DATA_URLS"
                )
                raise
        raise DatasetForbiddenDataURI()

    async def _table_exists(self, dataset: "SqlaTable") -> bool:
        """1:1 port of ``Database.has_table`` used by ``import_dataset``.

        Inspects the dataset's physical table on its backing database via a
        sync engine (run in a worker thread). Mirrors the original
        ``engine.has_table(table, schema)`` with the lowercase fallback.
        Raises on connection/inspection failure so the caller can apply the
        original "assume it exists" behaviour.
        """
        import asyncio

        import sqlalchemy as sa

        database = await self._get_dataset_database(dataset)
        if database is None:
            # No database -> treat as not existing so example data can load.
            return False

        if not getattr(database, "sqlalchemy_uri", ""):
            return False

        table_name = dataset.table_name
        schema = getattr(dataset, "schema", None) or None

        def _check_sync() -> bool:
            # ``get_sqla_engine`` (not a bare ``create_engine`` on the stored
            # URI) — the stored URI carries PASSWORD_MASK; the engine factory
            # restores the real password (sqlalchemy_uri_decrypted), 1:1 with
            # upstream ``database.has_table`` going through ``get_sqla_engine``.
            with database.get_sqla_engine() as engine:
                inspector = sa.inspect(engine)
                if inspector.has_table(table_name, schema):
                    return True
                return inspector.has_table(table_name.lower(), schema)

        return await asyncio.to_thread(_check_sync)

    async def _get_dataset_database(self, dataset: "SqlaTable") -> Any:
        """Resolve ``dataset.database`` without tripping MissingGreenlet.

        ``getattr(dataset, "database", None)`` does NOT suppress
        ``MissingGreenlet`` (it only suppresses ``AttributeError``) — an
        unloaded lazy relationship fires a sync SELECT on the async session.
        Check the loaded-state via the inspector first and fall back to an
        explicit async DAO fetch.
        """
        import sqlalchemy as sa

        if "database" not in sa.inspect(dataset).unloaded:
            return dataset.database
        assert self._dao is not None
        return await self._dao.get_database_by_id(dataset.database_id)

    async def _load_data(  # noqa: C901
        self,
        data_uri: str,
        dataset: "SqlaTable",
    ) -> None:
        """Load data from a URI into the dataset's table.

        Port of superset_old/commands/dataset/importers/v1/utils.py load_data().

        Restores the original SSRF protection: example URLs are first
        normalised (``examples://`` -> configured CDN) and then validated
        against ``DATASET_IMPORT_ALLOWED_DATA_URLS`` before any ``urlopen``.
        """
        import asyncio

        # Convert example URLs to align with configuration.
        data_uri = self._normalize_example_data_url(data_uri)

        # Validate against the allow-list (raises DatasetForbiddenDataURI).
        self._validate_data_uri(data_uri)

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
        database = await self._get_dataset_database(dataset)
        if database is None:
            logger.warning(
                "Cannot load data: database not found for dataset %s",
                dataset.table_name,
            )
            return

        table_name = dataset.table_name
        schema = getattr(dataset, "schema", None)
        catalog = getattr(dataset, "catalog", None)

        if not getattr(database, "sqlalchemy_uri", ""):
            logger.warning("Cannot load data: no sqlalchemy_uri on database")
            return

        def _load_sync() -> None:
            # ``get_sqla_engine`` restores the masked password — 1:1 with
            # upstream ``load_data``'s
            # ``database.get_sqla_engine(catalog=..., schema=...)``.
            with database.get_sqla_engine(catalog=catalog, schema=schema) as engine:
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

        await asyncio.to_thread(_load_sync)
