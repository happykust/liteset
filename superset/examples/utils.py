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
"""Load YAML-based example configs (COVID Vaccines, FCC Survey, etc.).

Reads ``superset/examples/configs/`` and imports databases, datasets,
charts, and dashboards into the metadata database using the sync
session from :mod:`superset.examples._ctx`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid as uuid_module
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)
YAML_EXTENSIONS = {".yaml", ".yml"}


def load_examples_from_configs(  # noqa: C901
    force_data: bool = False,
    load_test_data: bool = False,
) -> None:
    """Load all example configs from superset/examples/configs/.

    This is the Liteset equivalent of the original
    ``superset.examples.utils.load_examples_from_configs``.
    It directly inserts/updates records via the sync ``_ctx.session``
    instead of going through the async ImportModelsCommand infrastructure.
    """
    from superset.examples import _ctx

    contents = _load_contents(load_test_data)
    if not contents:
        logger.info("No YAML configs found to load")
        return

    logger.info("Loading %d YAML config files", len(contents))

    # Parse all YAML files
    configs: dict[str, dict[str, Any]] = {}
    for filename, raw_yaml in contents.items():
        try:
            configs[filename] = yaml.safe_load(raw_yaml)
        except yaml.YAMLError:
            logger.warning("Failed to parse YAML: %s", filename)
            continue

    # Import databases
    database_ids: dict[str, int] = {}
    for filename, config in configs.items():
        if filename.startswith("databases/") and config:
            db_rec = _import_database(config)
            if db_rec:
                database_ids[str(config.get("uuid", ""))] = db_rec.id

    # Import datasets
    examples_db = _ctx.get_example_database()
    dataset_info: dict[str, dict[str, Any]] = {}
    for filename, config in configs.items():
        if filename.startswith("datasets/") and config:
            db_uuid = config.get("database_uuid", "")
            if db_uuid not in database_ids:
                config["database_id"] = examples_db.id
            else:
                config["database_id"] = database_ids[db_uuid]

            if config.get("schema") is None:
                # Uses get_example_default_schema() to get the examples DB schema
                examples_eng = _ctx.get_example_engine(examples_db)
                config["schema"] = _ctx.get_schema(examples_eng)

            try:
                ds = _import_dataset(config, force_data=force_data)
            except Exception as exc:
                # Catches MultipleResultsFound for duplicate datasets.
                # Multiple results can be found for datasets. There was a bug in
                # load-examples that resulted in datasets being loaded with a NULL
                # schema. Users could then add a new dataset with the same name in
                # the correct schema, resulting in duplicates.
                from sqlalchemy.exc import MultipleResultsFound

                if isinstance(exc, MultipleResultsFound):
                    logger.warning(
                        "Multiple results found for dataset %s, skipping",
                        config.get("table_name", "unknown"),
                    )
                    continue
                raise
            if ds:
                dataset_info[str(config.get("uuid", ""))] = {
                    "datasource_id": ds.id,
                    "datasource_type": "table",
                    "datasource_name": ds.table_name,
                }

    # Import charts
    chart_ids: dict[str, int] = {}
    for filename, config in configs.items():
        if filename.startswith("charts/") and config:
            ds_uuid = config.get("dataset_uuid", "")
            if ds_uuid in dataset_info:
                config.update(dataset_info[ds_uuid])
                chart = _import_chart(config)
                if chart:
                    chart_ids[str(config.get("uuid", ""))] = chart.id

    # Import dashboards — wrap in try/except KeyError: continue to
    # skip dashboards whose chart/dataset ID references cannot be resolved.
    for filename, config in configs.items():
        if filename.startswith("dashboards/") and config:
            try:
                _import_dashboard(config, chart_ids, dataset_info)
            except KeyError:
                continue

    _ctx.session.flush()
    logger.info(
        "Config-based examples loaded: %d databases, %d datasets, %d charts",
        len(database_ids),
        len(dataset_info),
        len(chart_ids),
    )


def _load_contents(load_test_data: bool = False) -> dict[str, str]:
    """Traverse configs directory and load YAML file contents."""
    configs_dir = Path(__file__).parent / "configs"
    if not configs_dir.exists():
        return {}

    contents: dict[str, str] = {}
    test_re = re.compile(r"\.test\.|metadata\.yaml$")

    for path in sorted(configs_dir.rglob("*")):
        if path.suffix.lower() not in YAML_EXTENSIONS:
            continue
        if load_test_data and test_re.search(str(path)) is None:
            continue
        rel = str(path.relative_to(configs_dir))
        contents[rel] = path.read_text("utf-8")

    return contents


# ---------------------------------------------------------------------------
# Sync import helpers using _ctx.session
# ---------------------------------------------------------------------------


def _import_database(config: dict[str, Any]) -> Any:
    from superset.examples import _ctx
    from superset.models.core import Database

    db_name = config.get("database_name", "")
    existing = _ctx.session.query(Database).filter_by(database_name=db_name).first()
    if existing:
        return existing

    db = Database(
        database_name=db_name,
        sqlalchemy_uri=config.get("sqlalchemy_uri", ""),
        expose_in_sqllab=config.get("expose_in_sqllab", True),
        allow_run_async=config.get("allow_run_async", False),
        extra=config.get("extra", "{}")
        if isinstance(config.get("extra"), str)
        else "{}",
    )
    _ctx.session.add(db)
    _ctx.session.flush()
    return db


def _serialize_extra(value: Any) -> str | None:
    """Coerce a YAML ``extra`` value to the JSON string the Text column expects.

    Column/metric configs carry ``extra`` as a parsed dict; the ``extra`` Text
    column stores JSON (read back via ``CertificationMixin.get_extra_dict``).
    Mirrors the dataset-level guard and the v1 importer's serialization step.
    """
    if value is None or isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except TypeError:
        logger.info("Unable to encode `extra` field: %s", value)
        return None


def _import_dataset_columns(
    tbl: Any,
    columns_cfg: list[dict[str, Any]],
) -> None:
    """Import TableColumn rows for *tbl* from YAML columns config."""
    from superset.examples import _ctx
    from superset.models.connectors import TableColumn

    seen_cols: set[str] = set()
    for col_cfg in columns_cfg:
        col_name = col_cfg.get("column_name", "")
        if not col_name or col_name in seen_cols:
            continue
        seen_cols.add(col_name)
        tc = TableColumn(
            column_name=col_name,
            type=col_cfg.get("type", ""),
            is_dttm=col_cfg.get("is_dttm", False),
            groupby=col_cfg.get("groupby", True),
            filterable=col_cfg.get("filterable", True),
            expression=col_cfg.get("expression", ""),
            description=col_cfg.get("description", ""),
            verbose_name=col_cfg.get("verbose_name"),
            python_date_format=col_cfg.get("python_date_format"),
            extra=_serialize_extra(col_cfg.get("extra")),
            table_id=tbl.id,
        )
        _ctx.session.add(tc)


def _import_dataset_metrics(
    tbl: Any,
    metrics_cfg: list[dict[str, Any]],
) -> None:
    """Import SqlMetric rows for *tbl* from YAML metrics config."""
    from superset.examples import _ctx
    from superset.models.connectors import SqlMetric

    seen_metrics: set[str] = set()
    for met_cfg in metrics_cfg:
        met_name = met_cfg.get("metric_name", "")
        if not met_name or met_name in seen_metrics:
            continue
        seen_metrics.add(met_name)
        sm = SqlMetric(
            metric_name=met_name,
            expression=met_cfg.get("expression", ""),
            verbose_name=met_cfg.get("verbose_name"),
            metric_type=met_cfg.get("metric_type"),
            description=met_cfg.get("description", ""),
            d3format=met_cfg.get("d3format"),
            extra=_serialize_extra(met_cfg.get("extra")),
            warning_text=met_cfg.get("warning_text"),
            table_id=tbl.id,
        )
        _ctx.session.add(sm)


def _import_dataset(config: dict[str, Any], force_data: bool = False) -> Any:
    from superset.examples import _ctx
    from superset.models.connectors import SqlaTable

    table_name = config.get("table_name", "")
    schema = config.get("schema")
    db_id = config.get("database_id")

    # Compute raw_sql up-front so it is available in both the update and
    # create paths below.
    raw_sql = config.get("sql")
    if isinstance(raw_sql, str):
        raw_sql = raw_sql.strip() or None

    existing = (
        _ctx.session.query(SqlaTable)
        .filter_by(table_name=table_name, schema=schema, database_id=db_id)
        .first()
    )
    if existing:
        # Update all export_fields attributes and sync (delete-then-re-insert)
        # columns + metrics from config.
        # Without this, re-runs after YAML updates leave stale field values,
        # and new/removed columns or metrics are never applied.
        existing.table_name = table_name
        existing.schema = schema
        existing.sql = raw_sql
        existing.main_dttm_col = config.get("main_dttm_col")
        existing.description = config.get("description", "")
        existing.filter_select_enabled = config.get("filter_select_enabled", True)
        existing.extra = (
            config.get("extra", "{}") if isinstance(config.get("extra"), str) else "{}"
        )
        existing.template_params = _serialize_extra(config.get("template_params"))
        existing.fetch_values_predicate = config.get("fetch_values_predicate")
        existing.offset = config.get("offset", 0)
        existing.cache_timeout = config.get("cache_timeout")
        existing.normalize_columns = config.get("normalize_columns", False)
        existing.always_filter_main_dttm = config.get("always_filter_main_dttm", False)
        existing.catalog = config.get("catalog")
        existing.params = _serialize_extra(config.get("params"))
        existing.default_endpoint = config.get("default_endpoint")
        existing.folders = config.get("folders")

        # Sync columns and metrics: delete all existing then re-insert from
        # config (mirrors import_from_dict recursive=True, sync=["columns",
        # "metrics"] which removes children not present in the import set).
        from superset.models.connectors import SqlMetric, TableColumn

        (
            _ctx.session.query(TableColumn)
            .filter_by(table_id=existing.id)
            .delete(synchronize_session="fetch")
        )
        (
            _ctx.session.query(SqlMetric)
            .filter_by(table_id=existing.id)
            .delete(synchronize_session="fetch")
        )
        _ctx.session.flush()

        _import_dataset_columns(existing, config.get("columns", []))
        _import_dataset_metrics(existing, config.get("metrics", []))
        _ctx.session.flush()

        # The ``data_uri and (not table_exists or force_data)`` check runs for
        # existing metadata records too — if the physical table was dropped,
        # a plain re-run of load-examples must recreate it.
        data_uri = config.get("data")
        if data_uri:
            try:
                _examples_db = _ctx.get_example_database()
                _eng = _ctx.get_example_engine(_examples_db)
                _table_exists = _ctx.has_table(_eng, existing.table_name, schema)
            except Exception:
                logger.warning(
                    "Couldn't check if table %s exists, assuming it does",
                    existing.table_name,
                )
                _table_exists = True
            if not _table_exists or force_data:
                _load_dataset_data(
                    data_uri, existing, schema, config.get("columns", [])
                )
        return existing

    tbl = SqlaTable(
        table_name=table_name,
        schema=schema,
        database_id=db_id,
        sql=raw_sql,
        main_dttm_col=config.get("main_dttm_col"),
        description=config.get("description", ""),
        filter_select_enabled=config.get("filter_select_enabled", True),
        extra=config.get("extra", "{}")
        if isinstance(config.get("extra"), str)
        else "{}",
        template_params=_serialize_extra(config.get("template_params")),
        fetch_values_predicate=config.get("fetch_values_predicate"),
        offset=config.get("offset", 0),
        cache_timeout=config.get("cache_timeout"),
        normalize_columns=config.get("normalize_columns", False),
        always_filter_main_dttm=config.get("always_filter_main_dttm", False),
        is_sqllab_view=config.get("is_sqllab_view", False),
        # import_from_dict sets every export_fields key on the new record
        # too — keep parity with the UPDATE branch above.
        catalog=config.get("catalog"),
        params=_serialize_extra(config.get("params")),
        default_endpoint=config.get("default_endpoint"),
        folders=config.get("folders"),
    )
    if config.get("uuid"):
        tbl.uuid = uuid_module.UUID(config["uuid"])
    _ctx.session.add(tbl)
    _ctx.session.flush()

    _import_dataset_columns(tbl, config.get("columns", []))
    _import_dataset_metrics(tbl, config.get("metrics", []))

    _ctx.session.flush()

    # Load data from CSV if data URI is present.
    # Only load data when the physical table does not already exist, or when
    # force_data=True, to avoid overwriting pre-existing table data.
    data_uri = config.get("data")
    if data_uri:
        try:
            _examples_db = _ctx.get_example_database()
            _eng = _ctx.get_example_engine(_examples_db)
            _table_exists = _ctx.has_table(_eng, tbl.table_name, schema)
        except Exception:
            logger.warning(
                "Couldn't check if table %s exists, assuming it does", tbl.table_name
            )
            _table_exists = True
        if not _table_exists or force_data:
            _load_dataset_data(data_uri, tbl, schema, config.get("columns", []))

    return tbl


_VARCHAR_RE = re.compile(r"VARCHAR\((\d+)\)", re.IGNORECASE)

_SQLA_TYPE_MAP: dict[str, Any] = {}  # populated lazily


def _get_sqla_type_map() -> dict[str, Any]:
    """Lazily build the native-type -> SQLAlchemy type mapping."""
    if not _SQLA_TYPE_MAP:
        from sqlalchemy import (
            BigInteger,
            Boolean,
            Date,
            DateTime,
            Float,
            String,
            Text,
        )

        _SQLA_TYPE_MAP.update(
            {
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
        )
    return _SQLA_TYPE_MAP


def _get_sqla_type(native_type: str) -> Any:
    """Map a YAML column type string to a SQLAlchemy type instance."""
    from sqlalchemy import String

    type_map = _get_sqla_type_map()
    upper = native_type.upper()
    if upper in type_map:
        return type_map[upper]
    if match := _VARCHAR_RE.match(native_type):
        return String(int(match.group(1)))
    return String(255)


def _get_dtype(
    df: Any,
    columns_config: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build column_name -> SQLAlchemy type dict from YAML columns config."""
    dtype: dict[str, Any] = {}
    df_columns = set(df.columns)
    for col_cfg in columns_config:
        col_name = col_cfg.get("column_name", "")
        col_type = col_cfg.get("type", "")
        if col_name and col_name in df_columns and col_type:
            try:
                dtype[col_name] = _get_sqla_type(col_type)
            except Exception:
                logger.warning("Unknown column type %s for %s", col_type, col_name)
    return dtype


def _validate_data_uri(data_uri: str) -> None:
    """Validate that the data URI matches allowed URL patterns.

    Reads ``DATASET_IMPORT_ALLOWED_DATA_URLS`` from SupersetSettings.
    Defaults to ``[r".*"]`` (allow all) when not configured.
    """
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        allowed_urls: list[str] = getattr(
            settings, "dataset_import_allowed_data_urls", [r".*"]
        )
    except Exception:
        allowed_urls = [r".*"]

    for pattern in allowed_urls:
        try:
            if re.match(pattern, data_uri):
                return
        except re.error:
            logger.exception(
                "Invalid regular expression in DATASET_IMPORT_ALLOWED_DATA_URLS"
            )
            raise
    raise ValueError(
        f"Data URI is not allowed: {data_uri}. "
        "Check DATASET_IMPORT_ALLOWED_DATA_URLS configuration."
    )


def _load_dataset_data(
    data_uri: str,
    tbl: Any,
    schema: str | None,
    columns_config: list[dict[str, Any]] | None = None,
) -> None:
    """Download CSV from data URI and load into the examples database."""
    import gzip
    from urllib import request as urlrequest

    import pandas as pd
    from sqlalchemy import Date, DateTime

    from superset.examples import _ctx

    resolved_url = _resolve_data_uri(data_uri)

    # Validate URL against allowed patterns
    _validate_data_uri(resolved_url)

    logger.info("Downloading data from %s", resolved_url)

    try:
        data = urlrequest.urlopen(resolved_url)  # noqa: S310
        if resolved_url.endswith(".gz"):
            data = gzip.open(data)
        df = pd.read_csv(data, encoding="utf-8")
    except Exception:
        logger.warning("Failed to download data from %s", resolved_url)
        return

    # Compute dtype mapping from YAML column definitions
    dtype: dict[str, Any] = {}
    if columns_config:
        dtype = _get_dtype(df, columns_config)

    # Convert temporal columns (DATE/DATETIME) to pandas datetime
    for column_name, sqla_type in dtype.items():
        if isinstance(sqla_type, (Date, DateTime)):
            df[column_name] = pd.to_datetime(df[column_name])

    with _ctx.example_engine(_ctx.get_example_database()) as eng:
        df.to_sql(
            tbl.table_name,
            con=eng,
            schema=schema,
            if_exists="replace",
            chunksize=500,
            dtype=dtype if dtype else None,
            index=False,
            method="multi",
        )
    logger.info("Loaded %d rows into %s", len(df), tbl.table_name)


_EXAMPLES_DATA_REF: str = os.environ.get("SUPERSET_EXAMPLES_DATA_REF", "master")
_EXAMPLES_BASE_URL: str = os.environ.get(
    "SUPERSET_EXAMPLES_BASE_URL",
    f"https://cdn.jsdelivr.net/gh/apache-superset/examples-data@{_EXAMPLES_DATA_REF}/",
)


def _resolve_data_uri(uri: str) -> str:
    """Resolve examples:// protocol to CDN URL."""
    protocol = "examples://"
    if uri.startswith(protocol):
        return _EXAMPLES_BASE_URL + uri[len(protocol) :]
    return uri


def _import_chart(config: dict[str, Any]) -> Any:
    from superset.examples import _ctx
    from superset.models.slice import Slice

    slice_name = config.get("slice_name", "")
    # Deduplicate by UUID. A slice_name-based lookup lets a chart whose uuid
    # already exists (under a different name) fall through to an INSERT,
    # raising a ``slices.uuid`` UniqueViolation on re-run / shared uuid.
    chart_uuid = config.get("uuid")
    existing = (
        _ctx.session.query(Slice).filter_by(uuid=uuid_module.UUID(chart_uuid)).first()
        if chart_uuid
        else None
    )
    if existing:
        # Update in place
        existing.slice_name = slice_name or existing.slice_name
        existing.viz_type = config.get("viz_type", existing.viz_type)
        existing.datasource_id = config.get("datasource_id", existing.datasource_id)
        existing.datasource_type = config.get("datasource_type", "table")
        if config.get("params"):
            import json

            _params_val = config["params"]
            _params_str: str = (
                json.dumps(_params_val)
                if isinstance(_params_val, dict)
                else str(_params_val)
            )
            existing.params = _params_str  # type: ignore[assignment]
        return existing

    import json

    params = config.get("params", "{}")
    if isinstance(params, dict):
        params = json.dumps(params)

    query_context = config.get("query_context")
    if isinstance(query_context, dict):
        query_context = json.dumps(query_context)

    slc = Slice(
        slice_name=slice_name,
        viz_type=config.get("viz_type", "table"),
        datasource_id=config.get("datasource_id"),
        datasource_type=config.get("datasource_type", "table"),
        params=params,
        query_context=query_context,
        cache_timeout=config.get("cache_timeout"),
        description=config.get("description", ""),
        certified_by=config.get("certified_by"),
        certification_details=config.get("certification_details"),
    )
    if config.get("uuid"):
        slc.uuid = uuid_module.UUID(config["uuid"])
    _ctx.session.add(slc)
    _ctx.session.flush()
    return slc


def _import_dashboard(
    config: dict[str, Any],
    chart_ids: dict[str, int],
    dataset_info: dict[str, dict[str, Any]],
) -> Any:
    from superset.examples import _ctx
    from superset.models.dashboard import Dashboard
    from superset.utils import json

    slug = config.get("slug", "")
    # Deduplicate by UUID. A slug-based lookup breaks on the six example
    # dashboards shipping ``slug: null`` — every re-run of load-examples
    # would INSERT a duplicate row instead of updating.
    existing = (
        _ctx.session.query(Dashboard)
        .filter_by(uuid=uuid_module.UUID(config["uuid"]))
        .first()
        if config.get("uuid")
        else None
    )

    dash = existing or Dashboard()
    if not existing:
        _ctx.session.add(dash)

    dash.dashboard_title = config.get("dashboard_title", "")
    dash.slug = slug
    dash.published = True  # type: ignore[assignment]
    dash.css = config.get("css", "")
    dash.certified_by = config.get("certified_by")
    dash.certification_details = config.get("certification_details")
    dash.description = config.get("description", "")
    if config.get("uuid") and not existing:
        dash.uuid = uuid_module.UUID(config["uuid"])

    position = config.get("position", {})
    metadata = config.get("metadata", {})

    if isinstance(position, dict):
        # Update chartId refs in position to use new IDs
        _update_position_chart_ids(position, chart_ids)
        dash.position_json = json.dumps(position)  # type: ignore[assignment]

    if isinstance(metadata, dict):
        # Update ID refs in metadata (filter_scopes, expanded_slices, etc.)
        _update_metadata_chart_ids(metadata, position, chart_ids, dataset_info)
        dash.json_metadata = json.dumps(metadata)  # type: ignore[assignment]

    # Link charts to dashboard via dashboard_slices — insert-only, never
    # delete existing relationships.
    _ctx.session.flush()  # ensure dash.id is assigned before FK insert

    chart_uuids = _find_chart_uuids(position) if isinstance(position, dict) else set()
    slice_ids = [chart_ids[uuid] for uuid in chart_uuids if uuid in chart_ids]
    if slice_ids:
        from sqlalchemy.sql import select as sa_select

        from superset.models.dashboard import dashboard_slices

        existing_relationships = set(
            _ctx.session.execute(
                sa_select(
                    dashboard_slices.c.dashboard_id,
                    dashboard_slices.c.slice_id,
                )
            ).fetchall()
        )

        values = [
            {"dashboard_id": dash.id, "slice_id": chart_id}
            for chart_id in slice_ids
            if (dash.id, chart_id) not in existing_relationships
        ]
        if values:
            _ctx.session.execute(dashboard_slices.insert(), values)

    _ctx.session.flush()
    return dash


def _find_chart_uuids(position: dict[str, Any]) -> set[str]:
    """Extract chart UUIDs from dashboard position_json.

    Returns a set; a UUID appearing in several CHART components must not produce
    duplicate ``dashboard_slices`` rows (UniqueConstraint → IntegrityError).
    """
    uuids = set()
    for component in position.values():
        if isinstance(component, dict) and component.get("type") == "CHART":
            meta = component.get("meta", {})
            uuid = meta.get("uuid")
            if uuid:
                uuids.add(uuid)
    return uuids


def _build_old_to_new_id_map(
    position: dict[str, Any],
    chart_ids: dict[str, int],
) -> dict[int, int]:
    """Build old_chartId -> new_chartId map from position + uuid->id mapping."""
    id_map: dict[int, int] = {}
    for component in position.values():
        if (
            isinstance(component, dict)
            and component.get("type") == "CHART"
            and "meta" in component
        ):
            meta = component["meta"]
            uuid = meta.get("uuid")
            old_id = meta.get("chartId")
            if uuid and uuid in chart_ids and old_id is not None:
                id_map[old_id] = chart_ids[uuid]
    return id_map


def _update_position_chart_ids(
    position: dict[str, Any],
    chart_ids: dict[str, int],
) -> None:
    """Update chartId in position_json CHART components to use new IDs."""
    for component in position.values():
        if (
            isinstance(component, dict)
            and component.get("type") == "CHART"
            and "meta" in component
        ):
            meta = component["meta"]
            uuid = meta.get("uuid")
            if uuid and uuid in chart_ids:
                meta["chartId"] = chart_ids[uuid]
                meta["sliceId"] = chart_ids[uuid]


def _update_metadata_chart_ids(  # noqa: C901
    metadata: dict[str, Any],
    position: dict[str, Any],
    chart_ids: dict[str, int],
    dataset_info: dict[str, dict[str, Any]],
) -> None:
    """Update chart/dataset ID references in dashboard metadata."""
    import json as _json

    id_map = _build_old_to_new_id_map(position, chart_ids)

    # timed_refresh_immune_slices: uses id_map[old_id] (bare access — raises
    # KeyError for unmapped IDs). The caller must wrap in try/except
    # KeyError: continue to skip the whole dashboard when any chart is unmapped.
    if "timed_refresh_immune_slices" in metadata:
        metadata["timed_refresh_immune_slices"] = [
            id_map[old_id] for old_id in metadata["timed_refresh_immune_slices"]
        ]

    # expanded_slices: raises KeyError for unmapped IDs; caller must catch
    # KeyError to skip the dashboard.
    if "expanded_slices" in metadata:
        metadata["expanded_slices"] = {
            str(id_map[int(old_id)]): value
            for old_id, value in metadata["expanded_slices"].items()
        }

    if "default_filters" in metadata:
        # No try/except — let json.JSONDecodeError / TypeError propagate so the
        # caller's `except KeyError: continue` does NOT catch it and the import
        # aborts on malformed default_filters.
        default_filters = _json.loads(metadata["default_filters"])
        metadata["default_filters"] = _json.dumps(
            {
                str(id_map[int(old_id)]): value
                for old_id, value in default_filters.items()
                if int(old_id) in id_map
            }
        )

    if "filter_scopes" in metadata:
        # Outer keys and immune entries not in id_map are DROPPED (not kept
        # with stale IDs).
        metadata["filter_scopes"] = {
            str(id_map[int(old_id)]): columns
            for old_id, columns in metadata["filter_scopes"].items()
            if int(old_id) in id_map
        }
        # No defensive isinstance/"immune" guards — a malformed config raises
        # (KeyError/AttributeError) instead of being silently skipped.
        for columns in metadata["filter_scopes"].values():
            for attributes in columns.values():
                attributes["immune"] = [
                    id_map[old_id]
                    for old_id in attributes["immune"]
                    if old_id in id_map
                ]

    # Fix native filter dataset references
    for native_filter in metadata.get("native_filter_configuration", []):
        for target in native_filter.get("targets", []):
            dataset_uuid = target.pop("datasetUuid", None)
            if dataset_uuid:
                target["datasetId"] = dataset_info[dataset_uuid]["datasource_id"]
        scope_excluded = native_filter.get("scope", {}).get("excluded", [])
        if scope_excluded:
            # Drop IDs not in id_map; id_map.get(old_id, old_id) kept stale
            # IDs pointing to arbitrary charts in the target DB, causing
            # incorrect filter scoping.
            native_filter["scope"]["excluded"] = [
                id_map[old_id] for old_id in scope_excluded if old_id in id_map
            ]

    # Fix cross-filter scoping references
    cross_filter_global_config = metadata.get("global_chart_configuration", {})
    global_scope_excluded = cross_filter_global_config.get("scope", {}).get(
        "excluded", []
    )
    if global_scope_excluded:
        # Drop unmapped IDs.
        cross_filter_global_config["scope"]["excluded"] = [
            id_map[old_id] for old_id in global_scope_excluded if old_id in id_map
        ]

    if "chart_configuration" in metadata:
        new_chart_configuration: dict[str, Any] = {}
        for old_id_str, chart_config in metadata["chart_configuration"].items():
            try:
                old_id_int = int(old_id_str)
            except (TypeError, ValueError):
                continue

            new_id = id_map.get(old_id_int)
            if new_id is None:
                continue

            if isinstance(chart_config, dict):
                chart_config["id"] = new_id

                # Update cross filter scope excluded ids
                scope = chart_config.get("crossFilters", {}).get("scope", {})
                if isinstance(scope, dict):
                    excluded_scope = scope.get("excluded", [])
                    if excluded_scope:
                        # Drop unmapped IDs.
                        chart_config["crossFilters"]["scope"]["excluded"] = [
                            id_map[old_id]
                            for old_id in excluded_scope
                            if old_id in id_map
                        ]

            new_chart_configuration[str(new_id)] = chart_config

        metadata["chart_configuration"] = new_chart_configuration


def _read_yaml_contents(root: Path) -> dict[str, str]:
    """Walk *root* recursively and return ``{rel_path: raw_text}`` for YAML files."""
    contents: dict[str, str] = {}
    queue: list[Path] = [root]
    while queue:
        path_name = queue.pop()
        if path_name.is_dir():
            queue.extend(path_name.glob("*"))
        elif path_name.suffix.lower() in YAML_EXTENSIONS:
            with open(path_name) as fp:
                contents[str(path_name.relative_to(root))] = fp.read()
    return contents


def _strip_metadata_type(contents: dict[str, str]) -> None:
    """Remove the ``type`` key from ``metadata.yaml`` in *contents* in-place."""
    metadata_key = "metadata.yaml"
    raw_meta = contents.get(metadata_key, "{}")
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError:
        meta = {}
    if "type" in meta:
        del meta["type"]
    contents[metadata_key] = yaml.safe_dump(meta)


def _parse_yaml_configs(contents: dict[str, str]) -> dict[str, Any]:
    """Parse raw YAML strings and return only successfully-parsed dicts."""
    configs: dict[str, Any] = {}
    for filename, raw_yaml in contents.items():
        try:
            parsed = yaml.safe_load(raw_yaml)
            if isinstance(parsed, dict):
                configs[filename] = parsed
        except yaml.YAMLError:
            logger.warning("Failed to parse YAML: %s", filename)
    return configs


def _import_databases_from_configs(
    configs: dict[str, Any],
) -> dict[str, int]:
    """Import database configs; return ``{uuid_str: db_id}`` map."""
    database_ids: dict[str, int] = {}
    for filename, config in configs.items():
        if filename.startswith("databases/") and config:
            db_rec = _import_database(config)
            if db_rec:
                database_ids[str(config.get("uuid", ""))] = db_rec.id
    return database_ids


def _import_datasets_from_configs(
    configs: dict[str, Any],
    database_ids: dict[str, int],
    examples_db_id: int,
    force_data: bool,
) -> dict[str, Any]:
    """Import dataset configs; return ``{uuid_str: datasource_info}`` map."""
    from superset.examples import _ctx

    dataset_info: dict[str, Any] = {}
    examples_db = _ctx.get_example_database()
    # Resolve the examples DB schema once — always calls
    # ``get_example_default_schema()`` before importing any dataset.
    examples_eng = _ctx.get_example_engine(examples_db)
    default_schema = _ctx.get_schema(examples_eng)

    for filename, config in configs.items():
        if filename.startswith("datasets/") and config:
            db_uuid = config.get("database_uuid", "")
            config["database_id"] = (
                database_ids[db_uuid] if db_uuid in database_ids else examples_db_id
            )
            # Resolve schema=None to the engine default.
            if config.get("schema") is None:
                config["schema"] = default_schema
            ds = _import_dataset(config, force_data=force_data)
            if ds:
                dataset_info[str(config.get("uuid", ""))] = {
                    "datasource_id": ds.id,
                    "datasource_type": "table",
                    "datasource_name": ds.table_name,
                }
    return dataset_info


def _import_charts_from_configs(
    configs: dict[str, Any],
    dataset_info: dict[str, Any],
) -> dict[str, int]:
    """Import chart configs; return ``{uuid_str: chart_id}`` map."""
    chart_ids: dict[str, int] = {}
    for filename, config in configs.items():
        if filename.startswith("charts/") and config:
            ds_uuid = config.get("dataset_uuid", "")
            if ds_uuid in dataset_info:
                config.update(dataset_info[ds_uuid])
                chart = _import_chart(config)
                if chart:
                    chart_ids[str(config.get("uuid", ""))] = chart.id
    return chart_ids


def _import_dashboards_from_configs(
    configs: dict[str, Any],
    chart_ids: dict[str, int],
    dataset_info: dict[str, Any],
) -> None:
    """Import dashboard configs."""
    for filename, config in configs.items():
        if filename.startswith("dashboards/") and config:
            # Skip dashboards whose chart references cannot be resolved.
            try:
                _import_dashboard(config, chart_ids, dataset_info)
            except KeyError:
                continue


def load_configs_from_directory(
    root: Path,
    overwrite: bool = True,
    force_data: bool = False,
) -> None:
    """Load all the examples from a given directory.

    Reads YAML files relative to *root*, strips the ``type`` key from
    ``metadata.yaml`` (so any exported model can be imported directly from an
    unzipped bundle directory), then imports via :func:`_import_database`,
    :func:`_import_dataset`, :func:`_import_chart`, and
    :func:`_import_dashboard`.
    """
    from superset.examples import _ctx

    contents = _read_yaml_contents(root)

    # removing "type" from the metadata allows us to import any exported model
    # from the unzipped directory directly
    _strip_metadata_type(contents)

    # Re-use the same parse + import pipeline that load_examples_from_configs
    # uses, but with the caller-supplied contents dict instead of the built-in
    # examples/configs directory.
    configs = _parse_yaml_configs(contents)

    database_ids = _import_databases_from_configs(configs)

    examples_db = _ctx.get_example_database()
    dataset_info = _import_datasets_from_configs(
        configs, database_ids, examples_db.id, force_data
    )

    chart_ids = _import_charts_from_configs(configs, dataset_info)
    _import_dashboards_from_configs(configs, chart_ids, dataset_info)

    _ctx.session.commit()
