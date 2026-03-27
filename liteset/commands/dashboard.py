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
"""Dashboard command classes — business logic for dashboard CRUD and operations."""

from __future__ import annotations

import io
import logging
from typing import Any, TYPE_CHECKING

import yaml  # type: ignore[import-untyped]

from liteset.commands.base import AsyncBaseCommand
from liteset.exceptions import (
    CommandInvalidError,
    ImportFailedError,
    ObjectNotFoundError,
)
from liteset.importexport.export_base import AsyncExportModelsCommand
from liteset.importexport.import_base import AsyncImportModelsCommand
from liteset.utils import mask_uri_password

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from liteset.db.daos.dashboard import AsyncDashboardDAO, AsyncEmbeddedDashboardDAO
    from liteset.models.dashboard import Dashboard
    from liteset.models.embedded_dashboard import EmbeddedDashboard


class CreateDashboardCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager

    async def validate(self) -> None:
        slug = self._data.get("slug")
        if slug:
            is_unique = await self._dao.validate_slug_uniqueness(slug)
            if not is_unique:
                raise CommandInvalidError(f"slug '{slug}' already exists")

    async def run(self) -> "Dashboard":
        from liteset.models.dashboard import Dashboard

        dashboard = Dashboard(
            **{
                k: v
                for k, v in self._data.items()
                if k not in ("owners", "roles", "tags")
            }
        )
        if self._user_id is not None:
            dashboard.created_by_fk = self._user_id
            dashboard.changed_by_fk = self._user_id
        self._dao.session.add(dashboard)
        await self._dao.session.flush()

        # Resolve owners
        owner_ids = self._data.get("owners", [])
        if owner_ids and self._security_manager is not None:
            owners = []
            for oid in owner_ids:
                user = await self._security_manager.find_user_by_id(oid)
                if user:
                    owners.append(user)
            dashboard.owners = owners
        elif (
            not owner_ids
            and self._user_id is not None
            and self._security_manager is not None
        ):
            user = await self._security_manager.find_user_by_id(self._user_id)
            if user:
                dashboard.owners = [user]

        # Resolve roles
        role_ids = self._data.get("roles", [])
        if role_ids and self._security_manager is not None:
            roles = []
            for rid in role_ids:
                role = await self._security_manager.find_role_by_id(rid)
                if role:
                    roles.append(role)
            dashboard.roles = roles

        return dashboard


class UpdateDashboardCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.find_by_id(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dashboard, self._user_id
            )
        slug = self._data.get("slug")
        if slug is not None:
            is_unique = await self._dao.validate_update_slug_uniqueness(
                self._dashboard_id, slug
            )
            if not is_unique:
                raise CommandInvalidError(f"slug '{slug}' already exists")

    async def run(self) -> "Dashboard":
        assert self._dashboard is not None

        # Capture the old position_json before mutation so that
        # _process_tab_diff can compute the tab diff correctly.
        old_position_json = getattr(self._dashboard, "position_json", None)

        for key, value in self._data.items():
            if key in ("owners", "roles", "tags"):
                continue
            if hasattr(self._dashboard, key):
                setattr(self._dashboard, key, value)
        if self._user_id is not None:
            self._dashboard.changed_by_fk = self._user_id

        if "position_json" in self._data:
            await self._process_tab_diff(old_position_json)

        # Synchronise filter scopes, color maps and shared-label colors
        # when json_metadata is updated.
        if "json_metadata" in self._data:
            await self._dao.set_dash_metadata(self._dashboard, self._data)

        # Resolve owners
        owner_ids = self._data.get("owners", [])
        if owner_ids and self._security_manager is not None:
            owners = []
            for oid in owner_ids:
                user = await self._security_manager.find_user_by_id(oid)
                if user:
                    owners.append(user)
            self._dashboard.owners = owners

        # Resolve roles
        role_ids = self._data.get("roles", [])
        if role_ids and self._security_manager is not None:
            roles = []
            for rid in role_ids:
                role = await self._security_manager.find_role_by_id(rid)
                if role:
                    roles.append(role)
            self._dashboard.roles = roles

        await self._dao.session.flush()
        return self._dashboard

    async def _process_tab_diff(self, old_position_json: str | None) -> None:
        """Detect deleted tabs and deactivate report schedules anchored to them.

        Compares *old_position_json* (captured before the model was mutated)
        with the new value from ``self._data``.  Any tab IDs (keys starting
        with ``TAB-``) present in the old layout but absent from the new one
        are considered deleted.  Report schedules whose ``extra`` JSON
        references a deleted tab's anchor are deactivated (``active=False``).
        """
        import json as _json  # noqa: TID251

        assert self._dashboard is not None

        old_position = old_position_json or ""
        new_position = self._data.get("position_json", "")

        try:
            old_tabs = {
                k for k in _json.loads(old_position) if k.startswith("TAB-")
            } if old_position else set()
        except (ValueError, TypeError):
            old_tabs = set()

        try:
            new_tabs = {
                k for k in _json.loads(new_position) if k.startswith("TAB-")
            } if new_position else set()
        except (ValueError, TypeError):
            new_tabs = set()

        deleted_tabs = old_tabs - new_tabs
        if not deleted_tabs:
            return

        # Lazy-import the report DAO to avoid circular dependencies
        from liteset.db.daos.report import AsyncReportScheduleDAO

        report_dao = AsyncReportScheduleDAO(session=self._dao.session)
        reports = await report_dao.find_by_dashboard_id(self._dashboard_id)

        for report in reports:
            extra_raw = getattr(report, "extra", None) or "{}"
            try:
                extra = _json.loads(extra_raw)
            except (ValueError, TypeError):
                continue

            anchor = extra.get("anchor")
            if anchor and anchor in deleted_tabs:
                report.active = False  # type: ignore[assignment]
                logger.info(
                    "Deactivated report schedule %s (id=%s) — anchor tab "
                    "'%s' was removed from dashboard %s",
                    report.name,
                    report.id,
                    anchor,
                    self._dashboard_id,
                )


class DeleteDashboardCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.find_by_id(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dashboard, self._user_id
            )
        # Check for associated report schedules
        if hasattr(self._dao, "find_report_schedules_by_dashboard_id"):
            reports = await self._dao.find_report_schedules_by_dashboard_id(
                self._dashboard_id
            )
            if reports:
                raise CommandInvalidError(
                    "Cannot delete: associated report schedules exist"
                )

    async def run(self) -> None:
        assert self._dashboard is not None
        await self._dao.delete([self._dashboard])
        await self._dao.session.flush()


class BulkDeleteDashboardsCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_ids: list[int],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_ids = dashboard_ids
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboards: list[Any] = []

    async def validate(self) -> None:
        if not self._dashboard_ids:
            raise CommandInvalidError("No dashboard IDs provided")
        self._dashboards = await self._dao.find_by_ids(self._dashboard_ids)
        found_ids = {d.id for d in self._dashboards}
        missing = set(self._dashboard_ids) - found_ids
        if missing:
            raise ObjectNotFoundError("Dashboard", str(missing))
        # Ownership check
        if self._security_manager is not None:
            for dashboard in self._dashboards:
                await self._security_manager.raise_for_ownership(
                    dashboard, self._user_id
                )
        # Report schedule check
        if hasattr(self._dao, "find_report_schedules_by_dashboard_id"):
            for dashboard in self._dashboards:
                reports = await self._dao.find_report_schedules_by_dashboard_id(
                    dashboard.id
                )
                if reports:
                    raise CommandInvalidError(
                        f"Cannot delete dashboard {dashboard.id}: "
                        "associated report schedules exist"
                    )

    async def run(self) -> None:
        await self._dao.delete(self._dashboards)
        await self._dao.session.flush()


class CopyDashboardCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        data: dict[str, Any],
        current_user: Any | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._data = data
        self._current_user = current_user
        self._security_manager = security_manager
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if not self._data.get("dashboard_title"):
            raise CommandInvalidError("dashboard_title is required for copy")
        if not self._data.get("json_metadata"):
            raise CommandInvalidError("json_metadata is required for copy")

    async def run(self) -> "Dashboard":
        assert self._dashboard is not None
        new_dash = await self._dao.copy_dashboard(
            self._dashboard,
            self._data,
            current_user=self._current_user,
        )
        await self._dao.session.flush()
        return new_dash


class UpdateDashboardFiltersCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        data: dict[str, Any],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._data = data
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.find_by_id(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dashboard, self._user_id
            )

    async def run(self) -> "Dashboard":
        assert self._dashboard is not None
        import json  # noqa: TID251

        existing: dict[str, Any] = {}
        if self._dashboard.json_metadata:
            try:
                existing = json.loads(self._dashboard.json_metadata)
            except (json.JSONDecodeError, TypeError):
                pass

        nfc = existing.get("native_filter_configuration", [])
        # Process deleted — schema sends list[str] of filter IDs
        deleted_ids = set(self._data.get("deleted", []))
        nfc = [f for f in nfc if f["id"] not in deleted_ids]
        # Process modified — schema sends list[dict] with full filter objects
        for mod in self._data.get("modified", []):
            for i, f in enumerate(nfc):
                if f["id"] == mod["id"]:
                    nfc[i] = mod
                    break
        # Process reordered — schema sends list[str] of filter IDs in desired order
        if reordered := self._data.get("reordered", []):
            order_map = {rid: idx for idx, rid in enumerate(reordered)}
            nfc.sort(key=lambda f: order_map.get(f["id"], len(nfc)))
        existing["native_filter_configuration"] = nfc
        self._dashboard.json_metadata = json.dumps(existing)
        await self._dao.session.flush()
        return self._dashboard


class UpdateDashboardColorsCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        data: dict[str, Any],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._data = data
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.find_by_id(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dashboard, self._user_id
            )

    async def run(self) -> "Dashboard":
        assert self._dashboard is not None
        await self._dao.update_colors_config(self._dashboard, self._data)
        await self._dao.session.flush()
        return self._dashboard


class ExportDashboardsCommand(AsyncExportModelsCommand):
    _resource_type = "Dashboard"

    def __init__(
        self, model_ids: list[int], dao: AsyncDashboardDAO | None = None
    ) -> None:
        super().__init__(model_ids)
        self._dao = dao

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")
        dashboard = await self._dao.find_by_id(model_id)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", model_id)

        dash_data = {
            "dashboard_title": dashboard.dashboard_title,
            "slug": dashboard.slug,
            "position_json": dashboard.position_json,
            "css": dashboard.css,
            "json_metadata": dashboard.json_metadata,
            "published": dashboard.published,
            "uuid": str(dashboard.uuid) if dashboard.uuid else None,
        }
        files: list[tuple[str, str]] = [
            (
                f"dashboards/{dashboard.dashboard_title}.yaml",
                yaml.safe_dump(dash_data, sort_keys=False),
            ),
        ]
        # Bundle dependent resources: charts + datasets + databases
        seen_datasets: set[int] = set()
        seen_databases: set[int] = set()
        for chart in getattr(dashboard, "slices", []):
            chart_data = {
                "slice_name": chart.slice_name,
                "viz_type": chart.viz_type,
                "params": chart.params,
                "uuid": str(chart.uuid) if getattr(chart, "uuid", None) else None,
            }
            files.append(
                (
                    f"charts/{chart.slice_name}.yaml",
                    yaml.safe_dump(chart_data, sort_keys=False),
                )
            )
            ds = getattr(chart, "datasource", None)
            if ds and getattr(ds, "id", None) not in seen_datasets:
                seen_datasets.add(ds.id)
                ds_data = {
                    "table_name": getattr(ds, "table_name", ""),
                    "schema": getattr(ds, "schema", None),
                    "uuid": str(ds.uuid) if getattr(ds, "uuid", None) else None,
                }
                files.append(
                    (
                        f"datasets/{getattr(ds, 'table_name', 'unknown')}.yaml",
                        yaml.safe_dump(ds_data, sort_keys=False),
                    )
                )
                db = getattr(ds, "database", None)
                if db and getattr(db, "id", None) not in seen_databases:
                    seen_databases.add(db.id)
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


class ImportDashboardsCommand(AsyncImportModelsCommand):
    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncDashboardDAO | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao

    _IMPORT_ORDER = ("databases/", "datasets/", "charts/", "dashboards/")

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        for name, config in configs.items():
            if name.startswith("dashboards/") and not config.get("dashboard_title"):
                raise CommandInvalidError(f"Missing dashboard_title in {name}")

    async def run(self) -> None:
        """Override to ensure dependency order: databases -> datasets -> charts -> dashboards."""
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
        if file_name.startswith("charts/"):
            await self._import_chart(file_name, content)
            return
        if not file_name.startswith("dashboards/"):
            return

        from liteset.models.dashboard import Dashboard

        dashboard = Dashboard(
            dashboard_title=content.get("dashboard_title", ""),
            slug=content.get("slug"),
            css=content.get("css"),
            published=content.get("published", False),
        )
        self._dao.session.add(dashboard)
        await self._dao.session.flush()

    async def _check_existing(self, uuid_val: str) -> bool:
        """Check if a dashboard with this UUID already exists."""
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

    async def _import_chart(
        self, file_name: str, content: dict[str, Any]
    ) -> None:
        """Import a chart from the bundle (dependency of dashboards)."""
        try:
            from liteset.models.slice import Slice

            chart = Slice(
                slice_name=content.get("slice_name", ""),
                viz_type=content.get("viz_type", "table"),
                params=content.get("params", "{}"),
                datasource_id=content.get("datasource_id"),
                datasource_type=content.get("datasource_type", "table"),
            )
            uuid_val = content.get("uuid")
            if uuid_val and hasattr(chart, "uuid"):
                from uuid import UUID as _UUID

                chart.uuid = _UUID(uuid_val)
            self._dao.session.add(chart)
            await self._dao.session.flush()
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "Could not import dependent chart from %s: %s",
                file_name,
                exc,
            )
        except Exception as exc:
            raise ImportFailedError(
                f"Unexpected error importing chart from {file_name}: {exc}"
            ) from exc


def parse_tab_structure(position_json: str | None) -> list[dict[str, Any]]:
    """Extract tab structure from dashboard position_json.

    Returns list of {tab_id, tab_title, charts} dicts.
    Moved from controller to keep controllers thin.
    """
    import json  # noqa: TID251

    if not position_json:
        return []
    try:
        positions = json.loads(position_json)
    except (ValueError, TypeError):
        return []
    tabs = []
    for key, value in positions.items():
        if isinstance(value, dict) and value.get("type") == "TAB":
            meta = value.get("meta", {})
            chart_ids = [
                child.get("meta", {}).get("chartId")
                for child in positions.values()
                if isinstance(child, dict)
                and child.get("meta", {}).get("chartId")
                and child.get("parents")
                and key in child.get("parents", [])
            ]
            tabs.append(
                {
                    "tab_id": key,
                    "tab_title": meta.get("text", ""),
                    "charts": [c for c in chart_ids if c],
                }
            )
    return tabs


class UpsertEmbeddedDashboardCommand(AsyncBaseCommand["EmbeddedDashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        embedded_dao: AsyncEmbeddedDashboardDAO,
        dashboard_id: int,
        allowed_domains: list[str],
    ) -> None:
        self._dao = dao
        self._embedded_dao = embedded_dao
        self._dashboard_id = dashboard_id
        self._allowed_domains = allowed_domains
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)

    async def run(self) -> "EmbeddedDashboard":
        assert self._dashboard is not None
        embedded = await self._embedded_dao.upsert(
            self._dashboard.id,
            self._allowed_domains,
        )
        await self._embedded_dao.session.flush()
        return embedded


class DeleteEmbeddedDashboardCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        embedded_dao: AsyncEmbeddedDashboardDAO,
        dashboard_id: int,
    ) -> None:
        self._dao = dao
        self._embedded_dao = embedded_dao
        self._dashboard_id = dashboard_id
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)

    async def run(self) -> None:
        assert self._dashboard is not None
        embedded = await self._embedded_dao.find_by_dashboard_id(self._dashboard.id)
        if embedded:
            await self._embedded_dao.session.delete(embedded)
            await self._embedded_dao.session.flush()
