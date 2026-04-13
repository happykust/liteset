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
"""Chart command classes — business logic for chart CRUD and operations."""

from __future__ import annotations

import io
import json as _json
import logging
from typing import Any, TYPE_CHECKING
from uuid import UUID as _UUID

import yaml  # type: ignore[import-untyped]

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import (
    CommandInvalidError,
    ForbiddenError,
    ImportFailedError,
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

logger = logging.getLogger(__name__)

# Annotation type constant — matches superset.utils.core.AnnotationType.FORMULA
_ANNOTATION_TYPE_FORMULA = "FORMULA"

# Keys present in the standard export that are not needed
_EXPORT_REMOVE_KEYS = {"datasource_type", "datasource_name", "url_params"}

# Export version
EXPORT_VERSION = "1.0.0"


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.db.daos.chart import AsyncChartDAO
    from superset.models.slice import Slice


# ---------------------------------------------------------------------------
# Import helper functions (ported 1:1 from original)
# ---------------------------------------------------------------------------


def _get_filename(name: str, model_id: int | None) -> str:
    """Generate safe file name for export."""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (name or ""))
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


async def _import_chart(  # noqa: C901
    session: AsyncSession,
    config: dict[str, Any],
    overwrite: bool = False,
    security_manager: Any | None = None,
    current_user: Any | None = None,
) -> Slice:
    """Import a single chart from config dict.

    Ported 1:1 from superset_old/commands/chart/importers/v1/utils.py.
    Handles UUID-based dedup, annotation filtering, params JSON serialization,
    and owner management.
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

    # Build the chart model — use import_from_dict pattern or direct attribute set
    chart_id = config.pop("id", None)
    # Strip fields that are not model columns
    _NON_MODEL_FIELDS = {  # noqa: N806
        "dataset_uuid",
        "database_uuid",
        "version",
        "tags",
    }
    model_data = {k: v for k, v in config.items() if k not in _NON_MODEL_FIELDS}

    if chart_id is not None:
        # Update existing
        stmt = sa_select(Slice).where(Slice.id == chart_id)
        result = await session.execute(stmt)
        chart = result.scalars().one()
        for key, value in model_data.items():
            if hasattr(chart, key):
                setattr(chart, key, value)
    else:
        # Create new — filter to known columns
        chart = Slice(**{k: v for k, v in model_data.items() if hasattr(Slice, k)})
        session.add(chart)

    await session.flush()

    # Owner management
    if current_user is not None:
        await session.refresh(chart, ["owners"])
        if current_user not in chart.owners:
            chart.owners.append(current_user)

    return chart


async def _import_database(
    session: AsyncSession,
    config: dict[str, Any],
    overwrite: bool = False,
) -> Any:
    """Import a single database from config dict (UUID-based dedup).

    Returns the Database model with a valid .id (flushed).
    """
    from sqlalchemy import select as sa_select

    from superset.models.core import Database

    uuid_val = _UUID(str(config["uuid"]))
    stmt = sa_select(Database).where(Database.uuid == uuid_val)
    result = await session.execute(stmt)
    existing = result.scalars().one_or_none()

    if existing:
        if not overwrite:
            return existing
        # Update in-place
        for key in ("database_name", "sqlalchemy_uri"):
            if key in config and hasattr(existing, key):
                setattr(existing, key, config[key])
        await session.flush()
        return existing

    db = Database(
        database_name=config.get("database_name", ""),
        sqlalchemy_uri=config.get("sqlalchemy_uri", ""),
    )
    db.uuid = uuid_val  # type: ignore[assignment]
    session.add(db)
    await session.flush()
    return db


async def _import_dataset(
    session: AsyncSession,
    config: dict[str, Any],
    overwrite: bool = False,
) -> Any:
    """Import a single dataset from config dict (UUID-based dedup).

    Returns the SqlaTable model with a valid .id (flushed).
    """
    from sqlalchemy import select as sa_select

    from superset.models.connectors import SqlaTable

    uuid_val = _UUID(str(config["uuid"]))
    stmt = sa_select(SqlaTable).where(SqlaTable.uuid == uuid_val)
    result = await session.execute(stmt)
    existing = result.scalars().one_or_none()

    if existing:
        if not overwrite:
            return existing
        for key in ("table_name", "schema", "sql", "database_id"):
            if key in config and hasattr(existing, key):
                setattr(existing, key, config[key])
        await session.flush()
        return existing

    _NON_MODEL_FIELDS = {"database_uuid", "version", "uuid"}  # noqa: N806
    model_data = {
        k: v
        for k, v in config.items()
        if k not in _NON_MODEL_FIELDS and hasattr(SqlaTable, k)
    }
    dataset = SqlaTable(**model_data)
    dataset.uuid = uuid_val  # type: ignore[assignment]
    session.add(dataset)
    await session.flush()
    return dataset


class CreateChartCommand(AsyncBaseCommand["Slice"]):
    def __init__(
        self,
        dao: AsyncChartDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager

    async def validate(self) -> None:  # noqa: C901
        slice_name = self._data.get("slice_name")
        if not slice_name or not slice_name.strip():
            raise CommandInvalidError("slice_name is required")
        if not self._data.get("viz_type"):
            raise CommandInvalidError("viz_type is required")

        # Validate datasource exists
        datasource_id = self._data.get("datasource_id")
        datasource_type = self._data.get("datasource_type", "table")
        if datasource_id:
            if hasattr(self._dao, "find_datasource"):
                ds = await self._dao.find_datasource(datasource_id, datasource_type)
                if not ds:
                    raise CommandInvalidError(
                        f"Datasource {datasource_type}:{datasource_id} not found"
                    )
                self._data["datasource_name"] = ds.name

        # Validate dashboard access if dashboard IDs are provided
        dashboard_ids = self._data.get("dashboards", [])
        if dashboard_ids and self._security_manager is not None:
            from superset.models.dashboard import Dashboard

            user = (
                await self._security_manager.find_user_by_id(self._user_id)
                if self._user_id
                else None
            )
            if user is not None:
                for dash_id in dashboard_ids:
                    dashboard = await self._dao.session.get(Dashboard, dash_id)
                    if dashboard is None:
                        raise CommandInvalidError(f"Dashboard {dash_id} not found")
                    if hasattr(self._security_manager, "can_access_dashboard"):
                        has_access = await self._security_manager.can_access_dashboard(
                            dashboard, user=user
                        )
                        if not has_access:
                            raise ForbiddenError(
                                f"User does not have access to dashboard {dash_id}"
                            )

    async def run(self) -> "Slice":
        from datetime import datetime

        from superset.models.slice import Slice

        # Filter out relationship fields to avoid passing raw IDs to model constructor
        create_data = {
            k: v
            for k, v in self._data.items()
            if k not in ("owners", "tags", "dashboards")
        }
        chart = Slice(**create_data)
        if self._user_id is not None:
            chart.created_by_fk = self._user_id
            chart.changed_by_fk = self._user_id
            chart.last_saved_by_fk = self._user_id  # type: ignore[assignment]
        chart.last_saved_at = datetime.now()  # type: ignore[assignment]

        # Resolve owners
        owner_ids = self._data.get("owners", [])
        resolved_owner_ids: list[int] = []
        if owner_ids and self._security_manager is not None:
            owners = []
            for oid in owner_ids:
                user = await self._security_manager.find_user_by_id(oid)
                if user:
                    owners.append(user)
                    resolved_owner_ids.append(user.id)
            chart.owners = owners
        elif (
            not owner_ids
            and self._user_id is not None
            and self._security_manager is not None
        ):
            user = await self._security_manager.find_user_by_id(self._user_id)
            if user:
                chart.owners = [user]
                resolved_owner_ids.append(user.id)

        self._dao.session.add(chart)
        await self._dao.session.flush()

        # Add implicit type: and owner: tags (async port of ChartUpdater.after_insert)
        owner_ids = resolved_owner_ids
        await add_implicit_tags_after_insert(
            self._dao.session, "chart", chart.id, owner_ids
        )

        return chart


class UpdateChartCommand(AsyncBaseCommand["Slice"]):
    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._chart: Any | None = None

    async def validate(self) -> None:  # noqa: C901
        # Eager-load the M2M relationships that ``run()`` re-assigns so
        # that assignments don't trigger lazy reloads under asyncpg
        # (which crash with ``MissingGreenlet``).
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.slice import Slice

        stmt = (
            select(Slice)
            .where(Slice.id == self._chart_id)
            .options(
                selectinload(Slice.owners),
                selectinload(Slice.tags),
                selectinload(Slice.dashboards),
            )
        )
        result = await self._dao.session.execute(stmt)
        self._chart = result.scalars().unique().one_or_none()
        if not self._chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

        # If only query_context is being updated, skip ownership validation
        is_query_context_update = set(self._data.keys()) <= {
            "query_context",
            "query_context_generation",
        }
        if not is_query_context_update and self._security_manager is not None:
            await self._security_manager.raise_for_ownership(self._chart, self._user_id)

        # Validate datasource exists when datasource_id is being changed
        datasource_id = self._data.get("datasource_id")
        if datasource_id is not None:
            datasource_type = self._data.get("datasource_type", "table")
            if hasattr(self._dao, "find_datasource"):
                ds = await self._dao.find_datasource(datasource_id, datasource_type)
                if not ds:
                    raise CommandInvalidError(
                        f"Datasource {datasource_type}:{datasource_id} not found"
                    )

        # Validate dashboard access if dashboard IDs are provided
        dashboard_ids = self._data.get("dashboards", [])
        if dashboard_ids and self._security_manager is not None:
            from superset.models.dashboard import Dashboard

            # Dashboards are already pre-loaded in ``validate()``.
            existing_dashboard_ids = (
                {d.id for d in self._chart.dashboards}
                if hasattr(self._chart, "dashboards") and self._chart.dashboards
                else set()
            )
            new_dashboard_ids = set(dashboard_ids) - existing_dashboard_ids

            # Only validate access for NEW dashboard associations
            user = (
                await self._security_manager.find_user_by_id(self._user_id)
                if self._user_id
                else None
            )
            if user is not None:
                for dash_id in new_dashboard_ids:
                    dashboard = await self._dao.session.get(Dashboard, dash_id)
                    if dashboard is None:
                        raise CommandInvalidError(f"Dashboard {dash_id} not found")
                    if hasattr(self._security_manager, "can_access_dashboard"):
                        has_access = await self._security_manager.can_access_dashboard(
                            dashboard, user=user
                        )
                        if not has_access:
                            raise ForbiddenError(
                                f"User does not have access to dashboard {dash_id}"
                            )

    async def run(self) -> "Slice":
        from datetime import datetime

        assert self._chart is not None

        # Relationship fields must be resolved separately, not set via setattr
        _RELATIONSHIP_FIELDS = {"owners", "tags", "dashboards"}  # noqa: N806
        for key, value in self._data.items():
            if key in _RELATIONSHIP_FIELDS:
                continue
            if hasattr(self._chart, key):
                setattr(self._chart, key, value)

        # Resolve owners — ``validate()`` already pre-loaded the
        # collection via ``selectinload`` so the assignment below will
        # not trigger a lazy load.
        owner_ids = self._data.get("owners")
        if owner_ids is not None and self._security_manager is not None:
            owners = []
            for oid in owner_ids:
                user = await self._security_manager.find_user_by_id(oid)
                if user:
                    owners.append(user)
            self._chart.owners = owners

        # Resolve tags
        tag_ids = self._data.get("tags")
        if tag_ids is not None and hasattr(self._dao, "find_tags_by_ids"):
            self._chart.tags = await self._dao.find_tags_by_ids(tag_ids)

        # Resolve dashboards
        dashboard_ids = self._data.get("dashboards")
        if dashboard_ids is not None and hasattr(self._dao, "find_dashboards_by_ids"):
            self._chart.dashboards = await self._dao.find_dashboards_by_ids(
                dashboard_ids
            )

        if self._user_id is not None:
            self._chart.changed_by_fk = self._user_id
            self._chart.last_saved_by_fk = self._user_id
        self._chart.last_saved_at = datetime.now()
        await self._dao.session.flush()

        # Sync implicit owner: tags (async port of ChartUpdater.after_update).
        # Owners are already loaded from ``validate()``.
        owner_ids = (
            [o.id for o in self._chart.owners] if hasattr(self._chart, "owners") else []
        )
        await sync_owner_tags_after_update(
            self._dao.session, "chart", self._chart.id, owner_ids
        )

        return self._chart


class DeleteChartCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._security_manager = security_manager
        self._user_id = user_id
        self._chart: Any | None = None

    async def validate(self) -> None:
        self._chart = await self._dao.find_by_id(self._chart_id)
        if not self._chart:
            raise ObjectNotFoundError("Chart", self._chart_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(self._chart, self._user_id)
        # Check for report schedules referencing this chart
        if hasattr(self._dao, "find_report_schedules_by_chart_id"):
            reports = await self._dao.find_report_schedules_by_chart_id(self._chart_id)
            if reports:
                report_names = ", ".join(getattr(r, "name", str(r)) for r in reports)
                raise CommandInvalidError(
                    f"Cannot delete: associated report schedules exist: {report_names}"
                )

    async def run(self) -> None:
        assert self._chart is not None
        chart_id = self._chart.id
        # Remove implicit tags before deleting (async port of ChartUpdater.after_delete)
        await delete_tagged_objects(self._dao.session, "chart", chart_id)
        await self._dao.delete([self._chart])
        await self._dao.session.flush()


class BulkDeleteChartsCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_ids: list[int],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._chart_ids = chart_ids
        self._security_manager = security_manager
        self._user_id = user_id
        self._charts: list[Any] = []

    async def validate(self) -> None:
        if not self._chart_ids:
            raise CommandInvalidError("No chart IDs provided")
        self._charts = await self._dao.find_by_ids(self._chart_ids)
        found_ids = {int(c.id) for c in self._charts}
        missing = set(self._chart_ids) - found_ids
        if missing:
            raise ObjectNotFoundError("Chart", str(missing))
        if self._security_manager is not None:
            for chart in self._charts:
                await self._security_manager.raise_for_ownership(chart, self._user_id)

    async def run(self) -> None:
        await self._dao.delete(self._charts)
        await self._dao.session.flush()


class ExportChartsCommand(AsyncExportModelsCommand):
    """Export charts to a ZIP bundle.

    Ported 1:1 from superset_old/commands/chart/export.py.
    Includes all chart fields, dataset_uuid cross-reference, params JSON decode,
    and related dataset + database export.
    """

    _resource_type = "Slice"

    def __init__(self, model_ids: list[int], dao: AsyncChartDAO | None = None) -> None:
        super().__init__(model_ids)
        self._dao = dao

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        from sqlalchemy.orm import selectinload

        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")

        from sqlalchemy import select as sa_select

        from superset.models.connectors import SqlaTable
        from superset.models.slice import Slice

        # Eagerly load table -> database for the chart
        stmt = (
            sa_select(Slice)
            .where(Slice.id == model_id)
            .options(
                selectinload(Slice.table).selectinload(SqlaTable.database),
            )
        )
        result = await self._dao.session.execute(stmt)
        chart = result.scalars().one_or_none()
        if not chart:
            raise ObjectNotFoundError("Chart", model_id)

        # Build payload matching original export_to_dict + cleanup
        payload: dict[str, Any] = {
            "slice_name": chart.slice_name,
            "viz_type": chart.viz_type,
            "params": chart.params,
            "query_context": chart.query_context,
            "cache_timeout": chart.cache_timeout,
            "uuid": str(chart.uuid) if chart.uuid else None,
            "certified_by": chart.certified_by,
            "certification_details": chart.certification_details,
            "description": chart.description,
            "is_managed_externally": getattr(chart, "is_managed_externally", False),
            "external_url": getattr(chart, "external_url", None),
        }

        # Remove keys not needed in export
        for key in _EXPORT_REMOVE_KEYS:
            payload.pop(key, None)

        # Decode params from JSON string to dict for readable YAML
        if payload.get("params"):
            try:
                payload["params"] = _json.loads(payload["params"])
            except (_json.JSONDecodeError, TypeError):
                logger.info("Unable to decode `params` field: %s", payload["params"])

        payload["version"] = EXPORT_VERSION

        # Dataset UUID cross-reference (NOT integer datasource_id)
        if chart.table:
            payload["dataset_uuid"] = str(chart.table.uuid)

        file_name = _get_filename(chart.slice_name, chart.id)
        chart_yaml = yaml.safe_dump(payload, sort_keys=False)
        files: list[tuple[str, str]] = [(f"charts/{file_name}.yaml", chart_yaml)]

        # Bundle dependent resources: dataset + database
        if chart.table:
            ds = chart.table
            ds_data: dict[str, Any] = {
                "table_name": ds.table_name,
                "schema": getattr(ds, "schema", None),
                "sql": getattr(ds, "sql", None),
                "description": getattr(ds, "description", None),
                "cache_timeout": getattr(ds, "cache_timeout", None),
                "uuid": str(ds.uuid) if ds.uuid else None,
                "version": EXPORT_VERSION,
            }
            db = getattr(ds, "database", None)
            if db:
                ds_data["database_uuid"] = str(db.uuid) if db.uuid else None
                ds_file = _get_filename(ds.table_name, ds.id)
                files.append(
                    (
                        f"datasets/{ds_file}.yaml",
                        yaml.safe_dump(ds_data, sort_keys=False),
                    )
                )
                db_data: dict[str, Any] = {
                    "database_name": db.database_name,
                    "sqlalchemy_uri": mask_uri_password(
                        getattr(db, "sqlalchemy_uri", "")
                    ),
                    "uuid": str(db.uuid) if db.uuid else None,
                    "version": EXPORT_VERSION,
                }
                db_file = _get_filename(db.database_name, db.id)
                files.append(
                    (
                        f"databases/{db_file}.yaml",
                        yaml.safe_dump(db_data, sort_keys=False),
                    )
                )
        return files


class ImportChartsCommand(AsyncImportModelsCommand):
    """Import charts from a ZIP bundle.

    Ported 1:1 from superset_old/commands/chart/importers/v1/.
    Resolves dependencies: databases -> datasets -> charts.
    Handles UUID-based dedup, annotation filtering, params serialization,
    datasource cross-referencing via UUIDs, and owner management.
    """

    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncChartDAO | None = None,
        security_manager: Any | None = None,
        current_user: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao
        self._security_manager = security_manager
        self._current_user = current_user

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        for name, config in configs.items():
            if name.startswith("charts/") and not config.get("slice_name"):
                raise CommandInvalidError(f"Missing slice_name in {name}")

    async def _check_existing(self, uuid_val: str) -> bool:
        if self._dao is None:
            return False
        result = await self._dao.find_one_or_none(uuid=_UUID(uuid_val))
        return result is not None

    async def run(self) -> None:  # noqa: C901
        """Orchestrate import: databases -> datasets -> charts.

        Ported 1:1 from ImportChartsCommand._import in the original.
        """
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        configs = self._configs
        session = self._dao.session

        # 1. Discover datasets associated with charts
        dataset_uuids: set[str] = set()
        for file_name, config in configs.items():
            if file_name.startswith("charts/") and isinstance(config, dict):
                ds_uuid = config.get("dataset_uuid")
                if ds_uuid:
                    dataset_uuids.add(ds_uuid)

        # 2. Discover databases associated with needed datasets
        database_uuids: set[str] = set()
        for file_name, config in configs.items():
            if (
                file_name.startswith("datasets/")
                and isinstance(config, dict)
                and config.get("uuid") in dataset_uuids
            ):
                db_uuid = config.get("database_uuid")
                if db_uuid:
                    database_uuids.add(db_uuid)

        # 3. Import related databases (overwrite=False)
        database_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("databases/")
                and isinstance(config, dict)
                and config.get("uuid") in database_uuids
            ):
                db = await _import_database(session, config)
                database_ids[str(db.uuid)] = db.id

        # 4. Import datasets with correct parent ref (overwrite=False)
        datasets: dict[str, Any] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("datasets/")
                and isinstance(config, dict)
                and config.get("database_uuid") in database_ids
            ):
                config["database_id"] = database_ids[config["database_uuid"]]
                dataset = await _import_dataset(session, config)
                datasets[str(dataset.uuid)] = dataset

        # 5. Import charts with correct parent ref
        for file_name, config in configs.items():
            if (
                file_name.startswith("charts/")
                and isinstance(config, dict)
                and config.get("dataset_uuid") in datasets
            ):
                # Skip obsolete filter-box charts
                if config.get("viz_type") == "filter_box":
                    continue

                # Update datasource id, type, and name from resolved dataset
                dataset = datasets[config["dataset_uuid"]]
                dataset_dict = {
                    "datasource_id": dataset.id,
                    "datasource_type": "table",
                    "datasource_name": dataset.table_name,
                }
                config = update_chart_config_dataset(config, dataset_dict)

                await _import_chart(
                    session,
                    config,
                    overwrite=self._overwrite,
                    security_manager=self._security_manager,
                    current_user=self._current_user,
                )

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        # Not used — run() handles the full orchestration
        pass


class WarmUpChartCacheCommand(AsyncBaseCommand[dict[str, Any]]):
    """Warm up the cache for a chart by executing its query context.

    Ported 1:1 from superset_old/commands/chart/warm_up_cache.py.
    The original fetches the chart, loads its query_context JSON,
    builds a QueryContext, applies dashboard filters, and forces
    execution so the result is cached.

    This async version uses AsyncQueryContextProcessor directly.
    Legacy viz_types are not supported (they require Flask globals);
    only non-legacy charts with a query_context are warmed up.
    """

    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        dashboard_id: int | None = None,
        extra_filters: str | None = None,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._dashboard_id = dashboard_id
        self._extra_filters = extra_filters
        self._chart: Any | None = None

    async def validate(self) -> None:
        from sqlalchemy import select as sa_select
        from sqlalchemy.orm import selectinload

        from superset.models.connectors import SqlaTable
        from superset.models.slice import Slice

        # Eagerly load chart -> table (datasource) for query execution
        stmt = (
            sa_select(Slice)
            .where(Slice.id == self._chart_id)
            .options(
                selectinload(Slice.table).selectinload(SqlaTable.database),
            )
        )
        result = await self._dao.session.execute(stmt)
        self._chart = result.scalars().one_or_none()
        if not self._chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

    def _build_queries(
        self,
        qc_dict: dict[str, Any],
    ) -> list[Any]:
        """Build AsyncQueryObject list from stored query_context dict."""
        import json as _stdlib_json

        from superset.common.query_object import AsyncQueryObject

        queries: list[AsyncQueryObject] = []
        for q in qc_dict.get("queries", []):
            qo = AsyncQueryObject(
                **{
                    k: v
                    for k, v in q.items()
                    if k in AsyncQueryObject.__dataclass_fields__
                }
            )
            queries.append(qo)

        if not queries:
            raise CommandInvalidError("Chart query_context has no queries")

        # Apply dashboard extra filters if provided
        if self._extra_filters:
            extra = _stdlib_json.loads(self._extra_filters)
            for qo in queries:
                if hasattr(qo, "filters") and isinstance(qo.filters, list):
                    qo.filters.extend(extra)

        return queries

    async def run(self) -> dict[str, Any]:
        import json as _stdlib_json

        from superset.common.query_context import AsyncQueryContext
        from superset.common.query_context_processor import (
            AsyncQueryContextProcessor,
        )
        from superset.common.query_status import QueryStatus

        assert self._chart is not None
        chart = self._chart

        try:
            # Parse the chart's stored query_context JSON
            query_context_raw = chart.query_context
            if not query_context_raw:
                raise CommandInvalidError("Chart has no query_context")

            qc_dict = _stdlib_json.loads(query_context_raw)

            datasource = chart.table
            if not datasource:
                raise CommandInvalidError("Chart's datasource does not exist")

            queries = self._build_queries(qc_dict)

            query_context = AsyncQueryContext(
                datasource=datasource,
                queries=queries,
                force=True,
                slice_=chart,
            )

            from superset.config import SupersetSettings

            processor = AsyncQueryContextProcessor(
                datasource=datasource,
                settings=SupersetSettings(),
                security_manager=None,
                query_context=query_context,
            )

            payload = await processor.get_payload(
                query_objects=queries,
                force=True,
            )

            # Report the first error (matches original)
            for query_result in payload.get("queries", []):
                error = query_result.get("error")
                status = query_result.get("status")
                if error is not None:
                    return {
                        "chart_id": chart.id,
                        "viz_error": error,
                        "viz_status": status,
                    }

            return {
                "chart_id": chart.id,
                "viz_error": None,
                "viz_status": QueryStatus.SUCCESS,
            }

        except Exception as ex:
            logger.exception(
                "Error warming up cache for chart %s",
                self._chart_id,
            )
            return {
                "chart_id": chart.id,
                "viz_error": str(ex),
                "viz_status": None,
            }


class AddFavoriteChartCommand(AsyncBaseCommand[None]):
    """Add a chart to a user's favorites.

    Ported 1:1 from superset_old/commands/chart/fave.py.
    The original validates chart existence and ownership, then delegates
    to ChartDAO.add_favorite.
    """

    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        user_id: int,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._user_id = user_id

    async def validate(self) -> None:
        chart = await self._dao.find_by_id(self._chart_id)
        if not chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

    async def run(self) -> None:
        await self._dao.add_favorite(self._chart_id, user_id=self._user_id)


class RemoveFavoriteChartCommand(AsyncBaseCommand[None]):
    """Remove a chart from a user's favorites.

    Ported 1:1 from superset_old/commands/chart/unfave.py.
    The original validates chart existence and ownership, then delegates
    to ChartDAO.remove_favorite.
    """

    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        user_id: int,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._user_id = user_id

    async def validate(self) -> None:
        chart = await self._dao.find_by_id(self._chart_id)
        if not chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

    async def run(self) -> None:
        await self._dao.remove_favorite(self._chart_id, user_id=self._user_id)
