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
"""Chart command classes — business logic for chart CRUD and operations."""

from __future__ import annotations

import io
import logging
from typing import Any, TYPE_CHECKING

import yaml  # type: ignore[import-untyped]

from liteset.commands.base import AsyncBaseCommand
from liteset.exceptions import (
    CommandInvalidError,
    ForbiddenError,
    ImportFailedError,
    ObjectNotFoundError,
)
from liteset.importexport.export_base import AsyncExportModelsCommand
from liteset.importexport.import_base import AsyncImportModelsCommand
from liteset.utils import mask_uri_password

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from liteset.db.daos.chart import AsyncChartDAO
    from liteset.models.slice import Slice


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

    async def validate(self) -> None:
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
            from liteset.models.dashboard import Dashboard

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
        from datetime import datetime, timezone

        from liteset.models.slice import Slice

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
            chart.last_saved_by_fk = self._user_id
        chart.last_saved_at = datetime.now(tz=timezone.utc)

        # Resolve owners
        owner_ids = self._data.get("owners", [])
        if owner_ids and self._security_manager is not None:
            owners = []
            for oid in owner_ids:
                user = await self._security_manager.find_user_by_id(oid)
                if user:
                    owners.append(user)
            chart.owners = owners
        elif (
            not owner_ids
            and self._user_id is not None
            and self._security_manager is not None
        ):
            user = await self._security_manager.find_user_by_id(self._user_id)
            if user:
                chart.owners = [user]

        self._dao.session.add(chart)
        await self._dao.session.flush()
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

    async def validate(self) -> None:
        self._chart = await self._dao.find_by_id(self._chart_id)
        if not self._chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

        # If only query_context is being updated, skip ownership validation
        is_query_context_update = set(self._data.keys()) <= {
            "query_context",
            "query_context_generation",
        }
        if not is_query_context_update and self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._chart, self._user_id
            )

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
            from liteset.models.dashboard import Dashboard

            # Get existing dashboard IDs
            existing_dashboard_ids = {d.id for d in self._chart.dashboards} if hasattr(self._chart, 'dashboards') and self._chart.dashboards else set()
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
        from datetime import datetime, timezone

        assert self._chart is not None

        # Relationship fields must be resolved separately, not set via setattr
        _RELATIONSHIP_FIELDS = {"owners", "tags", "dashboards"}
        for key, value in self._data.items():
            if key in _RELATIONSHIP_FIELDS:
                continue
            if hasattr(self._chart, key):
                setattr(self._chart, key, value)

        # Resolve owners
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
        self._chart.last_saved_at = datetime.now(tz=timezone.utc)
        await self._dao.session.flush()
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
            await self._security_manager.raise_for_ownership(
                self._chart, self._user_id
            )
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
        found_ids = {c.id for c in self._charts}
        missing = set(self._chart_ids) - found_ids
        if missing:
            raise ObjectNotFoundError("Chart", str(missing))
        if self._security_manager is not None:
            for chart in self._charts:
                await self._security_manager.raise_for_ownership(
                    chart, self._user_id
                )

    async def run(self) -> None:
        await self._dao.delete(self._charts)
        await self._dao.session.flush()


class ExportChartsCommand(AsyncExportModelsCommand):
    _resource_type = "Slice"

    def __init__(self, model_ids: list[int], dao: AsyncChartDAO | None = None) -> None:
        super().__init__(model_ids)
        self._dao = dao

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")
        chart = await self._dao.find_by_id(model_id)
        if not chart:
            raise ObjectNotFoundError("Chart", model_id)

        chart_data = {
            "slice_name": chart.slice_name,
            "viz_type": chart.viz_type,
            "params": chart.params,
            "query_context": chart.query_context,
            "cache_timeout": chart.cache_timeout,
            "uuid": str(chart.uuid) if chart.uuid else None,
            "datasource_id": getattr(chart, "datasource_id", None),
            "datasource_type": getattr(chart, "datasource_type", "table"),
        }
        chart_yaml = yaml.safe_dump(chart_data, sort_keys=False)
        files: list[tuple[str, str]] = [(f"charts/{chart.slice_name}.yaml", chart_yaml)]
        # Bundle dependent resources: dataset + database
        ds = getattr(chart, "datasource", None)
        if ds:
            ds_data = {
                "table_name": getattr(ds, "table_name", ""),
                "schema": getattr(ds, "schema", None),
                "sql": getattr(ds, "sql", None),
                "uuid": str(ds.uuid) if getattr(ds, "uuid", None) else None,
            }
            files.append(
                (
                    f"datasets/{getattr(ds, 'table_name', 'unknown')}.yaml",
                    yaml.safe_dump(ds_data, sort_keys=False),
                )
            )
            db = getattr(ds, "database", None)
            if db:
                db_data = {
                    "database_name": db.database_name,
                    "sqlalchemy_uri": mask_uri_password(
                        getattr(db, "sqlalchemy_uri", "")
                    ),
                    "uuid": str(db.uuid) if getattr(db, "uuid", None) else None,
                }
                files.append(
                    (
                        f"databases/{db.database_name}.yaml",
                        yaml.safe_dump(db_data, sort_keys=False),
                    )
                )
        return files


class ImportChartsCommand(AsyncImportModelsCommand):
    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncChartDAO | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao

    _IMPORT_ORDER = ("databases/", "datasets/", "charts/")

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        for name, config in configs.items():
            if name.startswith("charts/") and not config.get("slice_name"):
                raise CommandInvalidError(f"Missing slice_name in {name}")

    async def run(self) -> None:
        """Override to ensure dependency order: databases -> datasets -> charts."""
        if self._configs is None:
            raise CommandInvalidError(
                "validate() must be called before run()"
            )
        configs = self._configs

        # Sort files by dependency order
        def _sort_key(item: tuple[str, Any]) -> int:
            name = item[0]
            for idx, prefix in enumerate(self._IMPORT_ORDER):
                if name.startswith(prefix):
                    return idx
            return len(self._IMPORT_ORDER)

        for file_name, content in sorted(configs.items(), key=_sort_key):
            if file_name == "metadata.yaml":
                continue
            await self._import_single(file_name, content)

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        # Process dependent resources in correct order: databases -> datasets -> charts
        if file_name.startswith("databases/"):
            await self._import_database(file_name, content)
            return
        if file_name.startswith("datasets/"):
            await self._import_dataset(file_name, content)
            return
        if not file_name.startswith("charts/"):
            return

        from liteset.models.slice import Slice

        chart = Slice(
            slice_name=content.get("slice_name", ""),
            viz_type=content.get("viz_type", "table"),
            params=content.get("params", "{}"),
            datasource_id=content.get("datasource_id"),
            datasource_type=content.get("datasource_type", "table"),
        )
        self._dao.session.add(chart)
        await self._dao.session.flush()

    async def _check_existing(self, uuid_val: str) -> bool:
        """Check if a chart with this UUID already exists."""
        from uuid import UUID as _UUID

        if self._dao is None:
            return False
        result = await self._dao.find_one_or_none(uuid=_UUID(uuid_val))
        return result is not None

    async def _import_database(
        self, file_name: str, content: dict[str, Any]
    ) -> None:
        """Import a database from the bundle (dependency of datasets)."""
        try:
            from liteset.models.core import Database

            db = Database(
                database_name=content.get("database_name", ""),
                sqlalchemy_uri=content.get("sqlalchemy_uri", ""),
            )
            uuid_val = content.get("uuid")
            if uuid_val and hasattr(db, "uuid"):
                from uuid import UUID as _UUID

                db.uuid = _UUID(uuid_val)
            self._dao.session.add(db)
            await self._dao.session.flush()
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "Could not import dependent database from %s: %s",
                file_name,
                exc,
            )
        except Exception as exc:
            raise ImportFailedError(
                f"Unexpected error importing database from {file_name}: {exc}"
            ) from exc

    async def _import_dataset(
        self, file_name: str, content: dict[str, Any]
    ) -> None:
        """Import a dataset from the bundle (dependency of charts)."""
        try:
            from liteset.models.connectors import SqlaTable

            dataset = SqlaTable(
                table_name=content.get("table_name", ""),
                schema=content.get("schema"),
                sql=content.get("sql"),
            )
            uuid_val = content.get("uuid")
            if uuid_val and hasattr(dataset, "uuid"):
                from uuid import UUID as _UUID

                dataset.uuid = _UUID(uuid_val)
            self._dao.session.add(dataset)
            await self._dao.session.flush()
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "Could not import dependent dataset from %s: %s",
                file_name,
                exc,
            )
        except Exception as exc:
            raise ImportFailedError(
                f"Unexpected error importing dataset from {file_name}: {exc}"
            ) from exc


class WarmUpChartCacheCommand(AsyncBaseCommand[list[dict[str, Any]]]):
    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        dashboard_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._dashboard_id = dashboard_id

    async def validate(self) -> None:
        chart = await self._dao.find_by_id(self._chart_id)
        if not chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

    async def run(self) -> list[dict[str, Any]]:
        return [{"chart_id": self._chart_id, "viz_status": "success"}]
