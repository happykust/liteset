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
                from sqlalchemy import inspect as sa_inspect

                config["schema"] = sa_inspect(_ctx.engine).default_schema_name

            ds = _import_dataset(config)
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

    # Import dashboards
    for filename, config in configs.items():
        if filename.startswith("dashboards/") and config:
            _import_dashboard(config, chart_ids, dataset_info)

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


def _import_dataset(config: dict[str, Any]) -> Any:
    from superset.examples import _ctx
    from superset.models.connectors import SqlaTable, SqlMetric, TableColumn

    table_name = config.get("table_name", "")
    schema = config.get("schema")
    db_id = config.get("database_id")

    existing = (
        _ctx.session.query(SqlaTable)
        .filter_by(table_name=table_name, schema=schema, database_id=db_id)
        .first()
    )
    if existing:
        return existing

    raw_sql = config.get("sql")
    if isinstance(raw_sql, str):
        raw_sql = raw_sql.strip() or None

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
        template_params=config.get("template_params"),
        fetch_values_predicate=config.get("fetch_values_predicate"),
        offset=config.get("offset", 0),
        cache_timeout=config.get("cache_timeout"),
        normalize_columns=config.get("normalize_columns", False),
        always_filter_main_dttm=config.get("always_filter_main_dttm", False),
        is_sqllab_view=config.get("is_sqllab_view", False),
    )
    if config.get("uuid"):
        tbl.uuid = uuid_module.UUID(config["uuid"])
    _ctx.session.add(tbl)
    _ctx.session.flush()

    # Import columns from YAML config (deduplicate by column_name)
    seen_cols: set[str] = set()
    for col_cfg in config.get("columns", []):
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
            extra=col_cfg.get("extra"),
            table_id=tbl.id,
        )
        _ctx.session.add(tc)

    # Import metrics from YAML config (deduplicate by metric_name)
    seen_metrics: set[str] = set()
    for met_cfg in config.get("metrics", []):
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
            extra=met_cfg.get("extra"),
            warning_text=met_cfg.get("warning_text"),
            table_id=tbl.id,
        )
        _ctx.session.add(sm)

    _ctx.session.flush()

    # Load data from CSV if data URI is present
    data_uri = config.get("data")
    if data_uri:
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


_EXAMPLES_BASE_URL = (
    "https://cdn.jsdelivr.net/gh/apache-superset/"
    f"examples-data@{os.environ.get('SUPERSET_EXAMPLES_DATA_REF', 'master')}/"
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
    existing = _ctx.session.query(Slice).filter_by(slice_name=slice_name).first()
    if existing:
        # Update in place
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
    from superset.models.slice import Slice
    from superset.utils import json

    slug = config.get("slug", "")
    existing = (
        _ctx.session.query(Dashboard).filter_by(slug=slug).first() if slug else None
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

    # Link charts to dashboard via dashboard_slices
    chart_uuids = _find_chart_uuids(position) if isinstance(position, dict) else []
    slice_ids = [chart_ids[uuid] for uuid in chart_uuids if uuid in chart_ids]
    if slice_ids:
        slices = _ctx.session.query(Slice).filter(Slice.id.in_(slice_ids)).all()
        dash.slices = slices

    _ctx.session.flush()
    return dash


def _find_chart_uuids(position: dict[str, Any]) -> list[str]:
    """Extract chart UUIDs from dashboard position_json."""
    uuids = []
    for component in position.values():
        if isinstance(component, dict) and component.get("type") == "CHART":
            meta = component.get("meta", {})
            uuid = meta.get("uuid")
            if uuid:
                uuids.append(uuid)
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

    if "timed_refresh_immune_slices" in metadata:
        metadata["timed_refresh_immune_slices"] = [
            id_map.get(old_id, old_id)
            for old_id in metadata["timed_refresh_immune_slices"]
        ]

    if "expanded_slices" in metadata:
        metadata["expanded_slices"] = {
            str(id_map.get(int(old_id), int(old_id))): value
            for old_id, value in metadata["expanded_slices"].items()
        }

    if "default_filters" in metadata:
        try:
            default_filters = _json.loads(metadata["default_filters"])
            metadata["default_filters"] = _json.dumps(
                {
                    str(id_map.get(int(old_id), int(old_id))): value
                    for old_id, value in default_filters.items()
                }
            )
        except (ValueError, TypeError):
            pass

    if "filter_scopes" in metadata:
        metadata["filter_scopes"] = {
            str(id_map.get(int(old_id), int(old_id))): columns
            for old_id, columns in metadata["filter_scopes"].items()
        }
        for columns in metadata["filter_scopes"].values():
            if isinstance(columns, dict):
                for attributes in columns.values():
                    if isinstance(attributes, dict) and "immune" in attributes:
                        attributes["immune"] = [
                            id_map.get(old_id, old_id)
                            for old_id in attributes["immune"]
                        ]

    # Fix native filter dataset references
    for native_filter in metadata.get("native_filter_configuration", []):
        for target in native_filter.get("targets", []):
            dataset_uuid = target.pop("datasetUuid", None)
            if dataset_uuid and dataset_uuid in dataset_info:
                target["datasetId"] = dataset_info[dataset_uuid]["datasource_id"]
        scope_excluded = native_filter.get("scope", {}).get("excluded", [])
        if scope_excluded:
            native_filter["scope"]["excluded"] = [
                id_map.get(old_id, old_id) for old_id in scope_excluded
            ]

    # Fix cross-filter scoping references
    cross_filter_global_config = metadata.get("global_chart_configuration", {})
    global_scope_excluded = cross_filter_global_config.get("scope", {}).get(
        "excluded", []
    )
    if global_scope_excluded:
        cross_filter_global_config["scope"]["excluded"] = [
            id_map.get(old_id, old_id) for old_id in global_scope_excluded
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
                        chart_config["crossFilters"]["scope"]["excluded"] = [
                            id_map.get(oid, oid) for oid in excluded_scope
                        ]

            new_chart_configuration[str(new_id)] = chart_config

        metadata["chart_configuration"] = new_chart_configuration


def load_configs_from_directory(
    root: Path,
    overwrite: bool = True,
    force_data: bool = False,
) -> None:
    """Load all the examples from a given directory.

    1:1 port of ``superset_old/examples/utils.load_configs_from_directory``.
    Reads YAML files relative to *root*, strips the ``type`` key from
    ``metadata.yaml`` (so any exported model can be imported directly from an
    unzipped bundle directory), then delegates to :func:`load_examples_from_configs`
    after temporarily replacing the configs directory.

    The original delegates to ``ImportExamplesCommand(contents, …).run()``;
    the port reuses the equivalent direct-import pipeline via
    :func:`_import_database`, :func:`_import_dataset`, :func:`_import_chart`,
    and :func:`_import_dashboard` that :func:`load_examples_from_configs`
    already calls.
    """
    contents: dict[str, str] = {}
    queue: list[Path] = [root]
    while queue:
        path_name = queue.pop()
        if path_name.is_dir():
            queue.extend(path_name.glob("*"))
        elif path_name.suffix.lower() in YAML_EXTENSIONS:
            with open(path_name) as fp:
                contents[str(path_name.relative_to(root))] = fp.read()

    # removing "type" from the metadata allows us to import any exported model
    # from the unzipped directory directly
    metadata_key = "metadata.yaml"
    raw_meta = contents.get(metadata_key, "{}")
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError:
        meta = {}
    if "type" in meta:
        del meta["type"]
    contents[metadata_key] = yaml.safe_dump(meta)

    # Re-use the same parse + import pipeline that load_examples_from_configs
    # uses, but with the caller-supplied contents dict instead of the built-in
    # examples/configs directory.
    configs: dict[str, Any] = {}
    for filename, raw_yaml in contents.items():
        try:
            parsed = yaml.safe_load(raw_yaml)
            if isinstance(parsed, dict):
                configs[filename] = parsed
        except yaml.YAMLError:
            logger.warning("Failed to parse YAML: %s", filename)
            continue

    from superset.examples import _ctx

    database_ids: dict[str, int] = {}
    for filename, config in configs.items():
        if filename.startswith("databases/") and config:
            db_rec = _import_database(config)
            if db_rec:
                database_ids[str(config.get("uuid", ""))] = db_rec.id

    examples_db = _ctx.get_example_database()
    dataset_info: dict[str, Any] = {}
    for filename, config in configs.items():
        if filename.startswith("datasets/") and config:
            db_uuid = config.get("database_uuid", "")
            config["database_id"] = (
                database_ids[db_uuid] if db_uuid in database_ids else examples_db.id
            )
            ds = _import_dataset(config)
            if ds:
                dataset_info[str(config.get("uuid", ""))] = {
                    "datasource_id": ds.id,
                    "datasource_type": "table",
                    "datasource_name": ds.table_name,
                }

    chart_ids: dict[str, int] = {}
    for filename, config in configs.items():
        if filename.startswith("charts/") and config:
            ds_uuid = config.get("dataset_uuid", "")
            if ds_uuid in dataset_info:
                config.update(dataset_info[ds_uuid])
                chart = _import_chart(config)
                if chart:
                    chart_ids[str(config.get("uuid", ""))] = chart.id

    for filename, config in configs.items():
        if filename.startswith("dashboards/") and config:
            _import_dashboard(config, chart_ids, dataset_info)

    _ctx.session.commit()
