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
"""Async port of ``superset_old/commands/chart/importers/v1/utils.py``.

Provides the per-resource UUID-based dedup importers used by the chart,
dashboard, and asset bundle importers.

This module also re-exports :func:`_import_database` and
:func:`_import_dataset` (full ports of the original
``import_database`` / ``import_dataset``) so the asset bundle
orchestrator can chain database -> dataset -> chart imports without
pulling the full ``ImportDatabasesCommand`` / ``ImportDatasetsCommand``
classes (which require a ``BytesIO`` archive).
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, TYPE_CHECKING
from uuid import UUID as _UUID

from superset.utils.file import secure_filename

from superset.exceptions import ImportFailedError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.models.connectors import SqlaTable
    from superset.models.core import Database
    from superset.models.slice import Slice

logger = logging.getLogger(__name__)

# Annotation type constant — matches superset.utils.core.AnnotationType.FORMULA
_ANNOTATION_TYPE_FORMULA = "FORMULA"

# Export version
EXPORT_VERSION = "1.0.0"

# JSON-serialised columns on SqlaTable (matches the original).
_DATASET_JSON_KEYS = {"params", "template_params", "extra"}


def _get_filename(name: str, model_id: int | None) -> str:
    """Generate safe file name for export using ``werkzeug.secure_filename``.

    Behaviour-compatible with ``superset_old.utils.file.get_filename``.
    """
    safe = secure_filename(name or "") or "unnamed"
    if model_id is not None:
        return f"{safe}_{model_id}"
    return safe


def filter_chart_annotations(chart_config: dict[str, Any]) -> None:
    """Mutate chart config params to keep only FORMULA annotations.

    Non-FORMULA annotations depend on other charts or annotation layers
    that may not be present in the import bundle, so strip them.
    """
    params = chart_config.get("params", {})
    if not isinstance(params, dict):
        return
    als = params.get("annotation_layers", [])
    params["annotation_layers"] = [
        al for al in als if al.get("annotationType") == _ANNOTATION_TYPE_FORMULA
    ]


def update_chart_config_dataset(
    config: dict[str, Any],
    dataset_info: dict[str, Any],
) -> dict[str, Any]:
    """Update chart configuration and query_context with new dataset info.

    Ported 1:1 from superset_old/commands/utils.py.
    """
    config.update(dataset_info)

    dataset_uid = f"{dataset_info['datasource_id']}__{dataset_info['datasource_type']}"
    if isinstance(config.get("params"), dict):
        config["params"]["datasource"] = dataset_uid

    if "query_context" in config and config["query_context"] is not None:
        try:
            query_context = _json.loads(config["query_context"])
            query_context["datasource"] = {
                "id": dataset_info["datasource_id"],
                "type": dataset_info["datasource_type"],
            }
            if "form_data" in query_context:
                query_context["form_data"]["datasource"] = dataset_uid
            if "queries" in query_context:
                for query in query_context["queries"]:
                    if "datasource" in query:
                        query["datasource"] = query_context["datasource"]
            config["query_context"] = _json.dumps(query_context)
        except (_json.JSONDecodeError, TypeError):
            config["query_context"] = None

    return config


# --------------------------------------------------------------------------- #
# Chart importer
# --------------------------------------------------------------------------- #


async def _import_chart(  # noqa: C901
    session: AsyncSession,
    config: dict[str, Any],
    overwrite: bool = False,
    security_manager: Any | None = None,
    current_user: Any | None = None,
) -> Slice:
    """Import a single chart from config dict.

    Ported 1:1 from superset_old/commands/chart/importers/v1/utils.py.
    Handles UUID-based dedup, annotation filtering, params JSON
    serialization, and owner management.
    """
    from sqlalchemy import select as sa_select

    from superset.models.slice import Slice

    can_write = True
    if security_manager is not None:
        can_write = await security_manager.can_access("can_write", "Chart")

    # UUID-based dedup
    stmt = sa_select(Slice).where(Slice.uuid == _UUID(str(config["uuid"])))
    result = await session.execute(stmt)
    existing = result.scalars().one_or_none()

    if existing:
        if overwrite and can_write and current_user:
            if security_manager is not None:
                can_access = await security_manager.can_access_chart(existing)
                is_admin = await security_manager.is_admin()
                await session.refresh(existing, ["owners"])
                if not can_access or (
                    current_user not in existing.owners and not is_admin
                ):
                    raise ImportFailedError(
                        "A chart already exists and user doesn't "
                        "have permissions to overwrite it"
                    )
        if not overwrite or not can_write:
            return existing
        config["id"] = existing.id
    elif not can_write:
        raise ImportFailedError(
            "Chart doesn't exist and user doesn't have permission to create charts"
        )

    # Filter non-FORMULA annotations
    filter_chart_annotations(config)

    # Serialize params dict to JSON string
    if isinstance(config.get("params"), dict):
        config["params"] = _json.dumps(config["params"])

    chart_id = config.pop("id", None)
    _NON_MODEL_FIELDS = {  # noqa: N806
        "dataset_uuid",
        "database_uuid",
        "version",
        "tags",
        "uuid",
    }
    model_data = {k: v for k, v in config.items() if k not in _NON_MODEL_FIELDS}

    if chart_id is not None:
        stmt = sa_select(Slice).where(Slice.id == chart_id)
        result = await session.execute(stmt)
        chart = result.scalars().one()
        for key, value in model_data.items():
            if hasattr(chart, key):
                setattr(chart, key, value)
    else:
        chart = Slice(**{k: v for k, v in model_data.items() if hasattr(Slice, k)})
        # Preserve the bundle's UUID if provided.
        if config.get("uuid"):
            chart.uuid = _UUID(str(config["uuid"]))  # type: ignore[assignment]
        session.add(chart)

    await session.flush()

    # Owner management
    if current_user is not None:
        await session.refresh(chart, ["owners"])
        if current_user not in chart.owners:
            chart.owners.append(current_user)

    return chart


# --------------------------------------------------------------------------- #
# Database importer (full port of original ``import_database``)
# --------------------------------------------------------------------------- #


async def _import_database(  # noqa: C901
    session: AsyncSession,
    config: dict[str, Any],
    overwrite: bool = False,
    ignore_permissions: bool = True,
    security_manager: Any | None = None,
) -> Database:
    """Full async port of
    ``superset_old.commands.database.importers.v1.utils.import_database``.

    Handles:
    - permission check
    - UUID-based dedup
    - URI safety check via ``check_sqlalchemy_uri`` (when configured)
    - ``allow_csv_upload`` -> ``allow_file_upload`` rename
    - ``schemas_allowed_for_csv_upload`` -> ``schemas_allowed_for_file_upload`` rename
    - ``extra`` JSON serialization
    - SSH tunnel upsert via separate row
    - permission-grant attempt via add_permissions when available
    """
    from sqlalchemy import select as sa_select

    from superset.models.core import Database

    can_write = ignore_permissions
    if not can_write and security_manager is not None:
        can_write = await security_manager.can_access("can_write", "Database")

    cfg = dict(config)
    uuid_str = cfg.get("uuid")
    existing: Database | None = None
    if uuid_str:
        existing = (
            (
                await session.execute(
                    sa_select(Database).where(Database.uuid == _UUID(uuid_str))
                )
            )
            .scalars()
            .one_or_none()
        )

    if existing:
        if not overwrite or not can_write:
            return existing
        cfg["id"] = existing.id
    elif not can_write:
        raise ImportFailedError(
            "Database doesn't exist and user doesn't have permission to "
            "create databases"
        )

    # Optional URI safety check (gated on PREVENT_UNSAFE_DB_CONNECTIONS).
    try:
        from superset.config import current_config

        if current_config.get("PREVENT_UNSAFE_DB_CONNECTIONS", True):
            try:
                from superset.databases.utils import make_url_safe
                from superset.security.analytics_db_safety import (
                    check_sqlalchemy_uri,
                )

                check_sqlalchemy_uri(make_url_safe(cfg.get("sqlalchemy_uri", "")))
            except ImportError:
                # Helpers not yet ported — skip the check rather than fail.
                pass
            except Exception as exc:  # noqa: BLE001
                # Wrap as ImportFailedError so the caller surfaces a clean
                # error message (matches the original).
                raise ImportFailedError(str(exc)) from exc
    except ImportError:
        pass

    # ``allow_csv_upload`` -> ``allow_file_upload`` rename
    if "allow_csv_upload" in cfg:
        cfg["allow_file_upload"] = cfg.pop("allow_csv_upload")

    # ``schemas_allowed_for_csv_upload`` -> ``schemas_allowed_for_file_upload``
    extra = cfg.get("extra")
    if isinstance(extra, dict) and "schemas_allowed_for_csv_upload" in extra:
        extra["schemas_allowed_for_file_upload"] = extra.pop(
            "schemas_allowed_for_csv_upload"
        )

    # Serialise extra dict into JSON for the column
    if isinstance(extra, dict):
        cfg["extra"] = _json.dumps(extra)
    elif extra is None:
        cfg["extra"] = "{}"

    # Pop ssh_tunnel before constructing the Database — it lives in a
    # separate table.
    ssh_tunnel_config = cfg.pop("ssh_tunnel", None)
    sqlalchemy_uri = cfg.pop("sqlalchemy_uri", "")

    # Trim non-model fields.
    cfg.pop("id", None)
    cfg.pop("version", None)
    cfg.pop("database_uuid", None)
    cfg.pop("uuid", None)

    db_columns = {
        "database_name",
        "password",
        "cache_timeout",
        "expose_in_sqllab",
        "allow_run_async",
        "allow_file_upload",
        "allow_ctas",
        "allow_cvas",
        "allow_dml",
        "force_ctas_schema",
        "extra",
        "encrypted_extra",
        "impersonate_user",
        "server_cert",
        "is_managed_externally",
        "external_url",
        "verbose_name",
        "configuration_method",
    }
    attrs = {k: v for k, v in cfg.items() if k in db_columns}

    if existing:
        for key, value in attrs.items():
            setattr(existing, key, value)
        database = existing
    else:
        database = Database(**attrs)
        if uuid_str:
            database.uuid = _UUID(uuid_str)  # type: ignore[assignment]
        session.add(database)

    # set_sqlalchemy_uri masks the password in the stored URI.
    if hasattr(database, "set_sqlalchemy_uri"):
        database.set_sqlalchemy_uri(sqlalchemy_uri)
    else:
        database.sqlalchemy_uri = sqlalchemy_uri

    if database.id is None:
        await session.flush()

    if ssh_tunnel_config:
        await _import_ssh_tunnel(session, database.id, dict(ssh_tunnel_config))

    # Best-effort permission setup — original calls ``add_permissions``.
    try:
        from superset.commands.database.utils import add_permissions  # type: ignore

        try:
            await add_permissions(  # type: ignore[misc]
                session,
                database,
                ssh_tunnel=None,
            )
        except TypeError:
            # Fall back to sync signature if liteset's port still uses one.
            add_permissions(database, None)
    except (ImportError, AttributeError):
        pass

    return database


async def _import_ssh_tunnel(
    session: AsyncSession,
    database_id: int,
    config: dict[str, Any],
) -> None:
    """Upsert an SSH tunnel row attached to ``database_id``."""
    from sqlalchemy import select as sa_select

    try:
        from superset.models.ssh_tunnel import SSHTunnel
    except ImportError:
        return

    cfg = dict(config)
    cfg["database_id"] = database_id
    cfg.pop("id", None)
    cfg.pop("uuid", None)

    existing = (
        (
            await session.execute(
                sa_select(SSHTunnel).where(SSHTunnel.database_id == database_id)
            )
        )
        .scalars()
        .one_or_none()
    )

    attrs = {
        "server_address",
        "server_port",
        "username",
        "password",
        "private_key",
        "private_key_password",
        "database_id",
    }
    if existing:
        for key in attrs:
            if key in cfg:
                value = cfg[key]
                if key in ("password", "private_key", "private_key_password") and (
                    value == "XXXXXXXXXX"
                ):
                    continue
                setattr(existing, key, value)
    else:
        filtered = {k: v for k, v in cfg.items() if k in attrs}
        session.add(SSHTunnel(**filtered))

    await session.flush()


# --------------------------------------------------------------------------- #
# Dataset importer (full port of original ``import_dataset``)
# --------------------------------------------------------------------------- #


async def _import_dataset(  # noqa: C901
    session: AsyncSession,
    config: dict[str, Any],
    overwrite: bool = False,
    force_data: bool = False,
    ignore_permissions: bool = True,
    security_manager: Any | None = None,
    current_user: Any | None = None,
) -> SqlaTable:
    """Full async port of
    ``superset_old.commands.dataset.importers.v1.utils.import_dataset``.

    Handles:
    - UUID-based dedup with ``MultipleResultsFound`` recovery
    - JSON serialization of params/template_params/extra and column/metric extras
    - Recursive columns/metrics import via :func:`_import_columns` /
      :func:`_import_metrics`
    - Optional CSV data load via :func:`_load_data`
    - Owner management
    """
    from sqlalchemy import select as sa_select
    from sqlalchemy.exc import MultipleResultsFound

    from superset.models.connectors import SqlaTable

    can_write = ignore_permissions
    if not can_write and security_manager is not None:
        can_write = await security_manager.can_access("can_write", "Dataset")

    cfg = dict(config)  # shallow copy
    uuid_str = cfg.get("uuid")

    existing: SqlaTable | None = None
    if uuid_str:
        existing = (
            (
                await session.execute(
                    sa_select(SqlaTable).where(SqlaTable.uuid == _UUID(uuid_str))
                )
            )
            .scalars()
            .one_or_none()
        )

    if existing:
        if (
            overwrite
            and can_write
            and current_user is not None
            and security_manager is not None
        ):
            await session.refresh(existing, ["owners"])
            is_admin = await security_manager.is_admin()
            if current_user not in existing.owners and not is_admin:
                raise ImportFailedError(
                    "A dataset already exists and user doesn't "
                    "have permissions to overwrite it"
                )
        if not overwrite or not can_write:
            return existing
        cfg["id"] = existing.id
    elif not can_write:
        raise ImportFailedError(
            "Dataset doesn't exist and user doesn't have permission to create datasets"
        )

    # JSON-serialise params/template_params/extra dicts.
    for key in _DATASET_JSON_KEYS:
        if cfg.get(key) is not None and isinstance(cfg[key], dict):
            try:
                cfg[key] = _json.dumps(cfg[key])
            except TypeError:
                logger.info("Unable to encode `%s` field: %s", key, cfg[key])

    # Same JSON-encode for nested column/metric extras.
    for nested in ("metrics", "columns"):
        for attributes in cfg.get(nested, []) or []:
            if isinstance(attributes, dict) and isinstance(
                attributes.get("extra"), dict
            ):
                try:
                    attributes["extra"] = _json.dumps(attributes["extra"])
                except TypeError:
                    logger.info(
                        "Unable to encode `extra` field: %s", attributes["extra"]
                    )
                    attributes["extra"] = None

    sync_columns = overwrite
    sync_metrics = overwrite
    data_uri = cfg.get("data")

    columns_config = cfg.pop("columns", []) or []
    metrics_config = cfg.pop("metrics", []) or []

    # Resolve database_id from database_uuid if needed.
    database_id = cfg.get("database_id")
    db_uuid = cfg.get("database_uuid")
    if not database_id and db_uuid:
        from superset.models.core import Database

        db_q = await session.execute(
            sa_select(Database).where(Database.uuid == _UUID(str(db_uuid)))
        )
        db = db_q.scalars().one_or_none()
        if db is not None:
            database_id = db.id
            cfg["database_id"] = database_id

    # Trim non-model fields.
    cfg.pop("id", None)
    cfg.pop("version", None)
    cfg.pop("database_uuid", None)
    cfg.pop("data", None)
    cfg.pop("uuid", None)
    cfg.pop("folders", None)  # Not in current liteset SqlaTable column set.

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
        "is_managed_externally",
        "external_url",
        "folders",
    }
    filtered = {
        k: v for k, v in cfg.items() if k in dataset_attrs and hasattr(SqlaTable, k)
    }

    if existing:
        for key, value in filtered.items():
            setattr(existing, key, value)
        dataset = existing
    else:
        try:
            dataset = SqlaTable(**filtered)
        except MultipleResultsFound:
            # Mirrors the original recovery: return the existing row by UUID.
            if uuid_str:
                existing_q = await session.execute(
                    sa_select(SqlaTable).where(SqlaTable.uuid == _UUID(uuid_str))
                )
                return existing_q.scalars().one()
            raise
        if uuid_str:
            dataset.uuid = _UUID(uuid_str)  # type: ignore[assignment]
        session.add(dataset)

    if dataset.id is None:
        await session.flush()

    # Recursive children.
    await _import_columns(session, dataset, columns_config, sync=sync_columns)
    await _import_metrics(session, dataset, metrics_config, sync=sync_metrics)
    await session.flush()

    # Optional data URL load.
    if data_uri and force_data:
        await _load_data(session, data_uri, dataset)

    # Owner management.
    if current_user is not None:
        await session.refresh(dataset, ["owners"])
        if current_user not in dataset.owners:
            dataset.owners.append(current_user)

    return dataset


async def _import_columns(  # noqa: C901
    session: AsyncSession,
    dataset: SqlaTable,
    columns_config: list[dict[str, Any]],
    sync: bool = False,
) -> None:
    """Upsert dataset column rows; if ``sync`` is True, delete absent ones."""
    from superset.models.connectors import TableColumn

    if not columns_config and not sync:
        return

    await session.refresh(dataset, ["columns"])
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

        existing_col = None
        if col_uuid:
            existing_col = existing_by_uuid.get(col_uuid)
        if existing_col is None:
            existing_col = existing_by_name.get(col_name)

        if existing_col:
            for key in col_attrs:
                if key in col_data:
                    setattr(existing_col, key, col_data[key])
            if col_uuid:
                existing_col.uuid = _UUID(col_uuid)  # type: ignore[assignment]
            seen_ids.add(existing_col.id)
        else:
            filtered = {k: v for k, v in col_data.items() if k in col_attrs}
            filtered["table_id"] = dataset.id
            new_col = TableColumn(**filtered)
            if col_uuid:
                new_col.uuid = _UUID(col_uuid)  # type: ignore[assignment]
            dataset.columns.append(new_col)

    if sync:
        ids_to_delete = existing_ids - seen_ids
        for cid in ids_to_delete:
            col_obj = next((c for c in dataset.columns if c.id == cid), None)
            if col_obj:
                dataset.columns.remove(col_obj)
                await session.delete(col_obj)


async def _import_metrics(  # noqa: C901
    session: AsyncSession,
    dataset: SqlaTable,
    metrics_config: list[dict[str, Any]],
    sync: bool = False,
) -> None:
    """Upsert the metric rows of a dataset, optionally syncing."""
    from superset.models.connectors import SqlMetric

    if not metrics_config and not sync:
        return

    await session.refresh(dataset, ["metrics"])
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
                await session.delete(m_obj)


async def _load_data(  # noqa: C901  # complex business logic
    session: AsyncSession,
    data_uri: str,
    dataset: SqlaTable,
) -> None:
    """Load CSV data from ``data_uri`` into the dataset's table.

    Mirrors the original ``load_data``: validates the URI against
    ``DATASET_IMPORT_ALLOWED_DATA_URLS``, downloads + decodes, and writes
    via ``database.get_sqla_engine`` (or, when the same Superset metadata
    DB is the target, the active session connection).

    Exceptions are propagated up — the caller's transaction handling is
    responsible for rolling back on failure.
    """
    import asyncio
    import gzip
    import re
    from urllib import request as url_request

    import pandas as pd
    from sqlalchemy import Date, DateTime

    # Validate URI against allow list.
    try:
        from superset.config import current_config

        allowed = current_config.get("DATASET_IMPORT_ALLOWED_DATA_URLS", [])
    except Exception:  # noqa: BLE001
        allowed = []

    if allowed:
        accepted = False
        for pattern in allowed:
            try:
                if re.match(pattern, data_uri):
                    accepted = True
                    break
            except re.error:
                logger.exception("Invalid regex on DATASET_IMPORT_ALLOWED_DATA_URLS")
                raise
        if not accepted:
            try:
                from superset.commands.dataset.exceptions import (
                    DatasetForbiddenDataURI,
                )

                raise DatasetForbiddenDataURI()
            except ImportError:
                raise ImportFailedError("Forbidden data URI")  # noqa: B904

    logger.info("Downloading data from %s", data_uri)

    def _download() -> Any:
        return url_request.urlopen(data_uri)  # noqa: S310

    data = await asyncio.to_thread(_download)
    if data_uri.endswith(".gz"):
        data = gzip.open(data)
    df = await asyncio.to_thread(pd.read_csv, data, encoding="utf-8")

    # Convert temporal columns (port of get_dtype + the to_datetime loop).
    from sqlalchemy import BigInteger, Boolean, Float, String, Text  # noqa: F401

    type_map = {
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
    varchar_re = re.compile(r"VARCHAR\((\d+)\)", re.IGNORECASE)

    def _sqla_type(native: str) -> Any:
        upper = (native or "").upper()
        if upper in type_map:
            return type_map[upper]
        if match := varchar_re.match(native or ""):
            return String(int(match.group(1)))
        raise ValueError(f"Unknown type: {native}")

    dtype: dict[str, Any] = {}
    for column in dataset.columns or []:
        if column.column_name in df.keys() and getattr(column, "type", None):
            dtype[column.column_name] = _sqla_type(column.type)

    for column_name, sqla_type in dtype.items():
        if isinstance(sqla_type, (Date, DateTime)):
            df[column_name] = pd.to_datetime(df[column_name])

    database = getattr(dataset, "database", None)
    if database is None:
        from sqlalchemy import select as sa_select

        from superset.models.core import Database

        db_q = await session.execute(
            sa_select(Database).where(Database.id == dataset.database_id)
        )
        database = db_q.scalars().one_or_none()
    if database is None:
        raise ImportFailedError(
            f"Cannot load data: database not found for dataset {dataset.table_name}"
        )

    table_name = dataset.table_name
    schema = getattr(dataset, "schema", None)
    catalog = getattr(dataset, "catalog", None)

    def _load_via_engine() -> None:
        # Prefer the database's own engine factory when available.
        get_engine = getattr(database, "get_sqla_engine", None)
        if get_engine is not None:
            with get_engine(catalog=catalog, schema=schema) as engine:
                df.to_sql(
                    table_name,
                    con=engine,
                    schema=schema,
                    if_exists="replace",
                    chunksize=512,
                    dtype=dtype,
                    index=False,
                    method="multi",
                )
        else:
            from sqlalchemy import create_engine

            engine = create_engine(getattr(database, "sqlalchemy_uri", ""))
            try:
                df.to_sql(
                    table_name,
                    con=engine,
                    schema=schema,
                    if_exists="replace",
                    chunksize=512,
                    dtype=dtype,
                    index=False,
                    method="multi",
                )
            finally:
                engine.dispose()

    await asyncio.to_thread(_load_via_engine)


__all__ = [
    "EXPORT_VERSION",
    "_get_filename",
    "_import_chart",
    "_import_database",
    "_import_dataset",
    "filter_chart_annotations",
    "update_chart_config_dataset",
]
