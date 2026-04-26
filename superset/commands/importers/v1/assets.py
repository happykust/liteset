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
"""Async port of ``superset_old.commands.importers.v1.assets.ImportAssetsCommand``.

This is the **all-in-one bundle importer** invoked by
``POST /api/v1/assets/import/``.  It accepts the parsed YAML configs
(plain ``{filename: dict}`` mapping) and imports every asset type in
dependency order: databases -> saved queries -> datasets -> charts
-> dashboards.

The original used four nested ``ImportXxxCommand`` classes via
Marshmallow validation; in liteset we delegate to the
:func:`_import_database`, :func:`_import_dataset`, :func:`_import_chart`
helpers already implemented in the per-resource command modules
(``superset.commands.{database,dataset,chart,dashboard}``).  This
mirrors the original's "share the per-resource ``import_xxx()``
helpers" pattern without duplicating logic.

Usage::

    cmd = ImportAssetsCommand(
        contents={"databases/foo.yaml": "...", ...},
        passwords={"databases/foo.yaml": "secret"},
        sparse=False,
        session=session,
        security_manager=sm,
        current_user=user,
    )
    await cmd.execute()
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from superset.commands.base import AsyncBaseCommand
from superset.commands.importers.v1.utils import (
    ASSET_SCHEMAS,
    get_resource_mappings_batched,
    load_configs,
    load_metadata,
    METADATA_FILE_NAME,
    validate_metadata_type,
)
from superset.exceptions import (
    CommandInvalidError,
    ImportFailedError,
)

logger = logging.getLogger(__name__)


class ImportAssetsCommand(AsyncBaseCommand[None]):
    """Import every asset type from a bundle (databases, datasets, charts,
    dashboards, saved queries).

    Mirrors ``superset_old.commands.importers.v1.assets.ImportAssetsCommand``:

    * Imports databases first (always with ``overwrite=True`` because the
      asset bundle is the source of truth for source-controlled assets).
    * Then imports saved queries, then datasets (linked by ``database_uuid``),
      then charts (linked by ``dataset_uuid``), then dashboards (linked by
      chart UUIDs in their ``position`` and dataset UUIDs in their native
      filter targets).
    * For dashboards, refreshes the ``dashboard_slices`` M2M with the new
      ``(dashboard_id, chart_id)`` pairs and triggers the legacy filter-box
      migration.
    """

    def __init__(
        self,
        contents: dict[str, str],
        session: AsyncSession,
        passwords: dict[str, str] | None = None,
        ssh_tunnel_passwords: dict[str, str] | None = None,
        ssh_tunnel_private_keys: dict[str, str] | None = None,
        ssh_tunnel_priv_key_passwords: dict[str, str] | None = None,
        sparse: bool = False,
        security_manager: Any | None = None,
        current_user: Any | None = None,
    ) -> None:
        self.contents = contents
        self.session = session
        self.passwords = passwords or {}
        self.ssh_tunnel_passwords = ssh_tunnel_passwords or {}
        self.ssh_tunnel_private_keys = ssh_tunnel_private_keys or {}
        self.ssh_tunnel_priv_key_passwords = ssh_tunnel_priv_key_passwords or {}
        self.sparse = sparse
        self.security_manager = security_manager
        self.current_user = current_user

        self._configs: dict[str, Any] = {}

    async def validate(self) -> None:
        """Read & validate the bundle metadata + every YAML entry.

        Aggregates all schema errors and raises a single
        :class:`CommandInvalidError` with the combined message — matches
        the original Marshmallow ``raise CommandInvalidError(..., exceptions)``
        contract.
        """
        exceptions: list[Exception] = []

        # verify that the metadata file is present and valid
        try:
            metadata = load_metadata(self.contents)
        except CommandInvalidError as exc:
            exceptions.append(exc)
            metadata = None

        validate_metadata_type(metadata, "assets", exceptions)

        self._configs = await load_configs(
            self.contents,
            ASSET_SCHEMAS,
            self.passwords,
            exceptions,
            self.ssh_tunnel_passwords,
            self.ssh_tunnel_private_keys,
            self.ssh_tunnel_priv_key_passwords,
            self.session,
        )

        if exceptions:
            messages = [str(ex) for ex in exceptions]
            raise CommandInvalidError(
                "Error importing assets: " + "; ".join(messages),
                exceptions,
            )

    async def run(self) -> None:
        """Execute the import in dependency order — 1:1 port of the
        original ``ImportAssetsCommand._import``.
        """
        try:
            await self._import(self._configs, self.sparse)
        except CommandInvalidError:
            raise
        except Exception as ex:  # noqa: BLE001
            raise ImportFailedError() from ex

    async def _import(  # noqa: C901
        self,
        configs: dict[str, Any],
        sparse: bool,
    ) -> None:
        """Run the import in dependency order — 1:1 port of the original.

        Order matches
        ``superset_old.commands.importers.v1.assets.ImportAssetsCommand._import``:
        databases -> saved queries -> datasets -> charts -> dashboards.
        """
        from superset.commands.chart.importers.v1.utils import (
            _import_chart,
            _import_database,
            _import_dataset,
            update_chart_config_dataset,
        )
        from superset.commands.dashboard.importers.v1.utils import (
            _import_dashboard,
            find_chart_uuids,
            update_id_refs,
        )
        from superset.commands.query.importers.v1.utils import import_saved_query
        from superset.models.connectors import SqlaTable
        from superset.models.core import Database
        from superset.models.dashboard import dashboard_slices
        from superset.models.slice import Slice

        # ------------------------------------------------------------------
        # Sparse mode pre-population
        # ------------------------------------------------------------------
        chart_ids: dict[str, int] = {}
        database_ids: dict[str, int] = {}
        dataset_info: dict[str, dict[str, Any]] = {}

        if sparse:
            chart_ids = await get_resource_mappings_batched(self.session, Slice)
            database_ids = await get_resource_mappings_batched(self.session, Database)
            dataset_info = await get_resource_mappings_batched(
                self.session,
                SqlaTable,
                value_func=lambda ds: {
                    "datasource_id": ds.id,
                    "datasource_type": getattr(ds, "datasource_type", "table"),
                    "datasource_name": ds.datasource_name,
                },
            )

        # ------------------------------------------------------------------
        # 1. Databases (always overwrite=True for asset bundles)
        # ------------------------------------------------------------------
        for file_name, config in configs.items():
            if file_name.startswith("databases/"):
                database = await _import_database(
                    self.session,
                    dict(config),
                    overwrite=True,
                    ignore_permissions=True,
                    security_manager=self.security_manager,
                )
                database_ids[str(database.uuid)] = int(database.id)

        # ------------------------------------------------------------------
        # 2. Saved queries
        # ------------------------------------------------------------------
        for file_name, config in configs.items():
            if file_name.startswith("queries/"):
                db_uuid = config.get("database_uuid")
                if db_uuid and db_uuid in database_ids:
                    sq_cfg = dict(config)
                    sq_cfg["db_id"] = database_ids[db_uuid]
                    await import_saved_query(self.session, sq_cfg, overwrite=True)

        # ------------------------------------------------------------------
        # 3. Datasets
        # ------------------------------------------------------------------
        for file_name, config in configs.items():
            if file_name.startswith("datasets/"):
                db_uuid = config.get("database_uuid")
                if db_uuid and db_uuid in database_ids:
                    ds_cfg = dict(config)
                    ds_cfg["database_id"] = database_ids[db_uuid]
                    dataset = await _import_dataset(
                        self.session,
                        ds_cfg,
                        overwrite=True,
                        ignore_permissions=True,
                        security_manager=self.security_manager,
                        current_user=self.current_user,
                    )
                    dataset_info[str(dataset.uuid)] = {
                        "datasource_id": dataset.id,
                        "datasource_type": getattr(dataset, "datasource_type", "table"),
                        "datasource_name": dataset.datasource_name,
                    }

        # ------------------------------------------------------------------
        # 4. Charts
        # ------------------------------------------------------------------
        charts: list[Slice] = []
        for file_name, config in configs.items():
            if file_name.startswith("charts/"):
                ds_uuid = config.get("dataset_uuid")
                if ds_uuid and ds_uuid in dataset_info:
                    cfg = update_chart_config_dataset(
                        dict(config), dataset_info[ds_uuid]
                    )
                    chart = await _import_chart(
                        self.session,
                        cfg,
                        overwrite=True,
                        security_manager=self.security_manager,
                        current_user=self.current_user,
                    )
                    charts.append(chart)
                    chart_ids[str(chart.uuid)] = int(chart.id)

        # ------------------------------------------------------------------
        # 5. Dashboards
        # ------------------------------------------------------------------
        from superset.migrations.shared.native_filters import migrate_dashboard

        for file_name, config in configs.items():
            if file_name.startswith("dashboards/"):
                d_cfg = update_id_refs(dict(config), chart_ids, dataset_info)
                dashboard = await _import_dashboard(
                    self.session,
                    d_cfg,
                    overwrite=True,
                    security_manager=self.security_manager,
                    current_user=self.current_user,
                )

                # set ref in the dashboard_slices table — matches original 1:1
                # (uses ``break`` on first missing UUID, mirroring the original).
                dashboard_chart_ids: list[dict[str, int]] = []
                for uuid_str in find_chart_uuids(d_cfg.get("position", {})):
                    if uuid_str not in chart_ids:
                        break
                    dashboard_chart_ids.append(
                        {
                            "dashboard_id": int(dashboard.id),
                            "slice_id": chart_ids[uuid_str],
                        }
                    )

                # Replace existing dashboard<->chart links with the imported set
                # (matches the ``delete -> insert`` pattern in the original).
                await self.session.execute(
                    delete(dashboard_slices).where(
                        dashboard_slices.c.dashboard_id == dashboard.id
                    )
                )
                if dashboard_chart_ids:
                    await self.session.execute(
                        insert(dashboard_slices).values(dashboard_chart_ids)
                    )

                # Migrate any filter-box charts to native dashboard filters.
                await self.session.refresh(dashboard, ["slices"])
                migrate_dashboard(dashboard)

        # ------------------------------------------------------------------
        # 6. Cleanup obsolete filter-box charts
        # ------------------------------------------------------------------
        for chart in charts:
            if getattr(chart, "viz_type", None) == "filter_box":
                await self.session.delete(chart)

        await self.session.flush()

    # ------------------------------------------------------------------ #
    # Helpers (legacy — kept as compat shims in case external callers
    # invoke them; new code uses the per-resource async helpers in
    # ``superset.commands.chart.importers.v1.utils``).
    # ------------------------------------------------------------------ #

    async def _import_database(self, config: dict[str, Any]) -> Any:
        """Upsert a Database row via the per-resource async helper."""
        from superset.commands.chart.importers.v1.utils import _import_database

        return await _import_database(
            self.session,
            dict(config),
            overwrite=True,
            ignore_permissions=True,
            security_manager=self.security_manager,
        )

    async def _import_database_legacy(  # noqa: C901  # complex business logic
        self, config: dict[str, Any]
    ) -> Any:  # pragma: no cover - kept for reference
        """Legacy in-line port — superseded by ``_import_database`` above."""
        from uuid import UUID as _UUID

        from sqlalchemy import select

        from superset.models.core import Database

        cfg = dict(config)
        uuid_str = cfg.get("uuid")
        existing: Database | None = None
        if uuid_str:
            existing = (
                (
                    await self.session.execute(
                        select(Database).where(Database.uuid == _UUID(uuid_str))
                    )
                )
                .scalars()
                .one_or_none()
            )

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
            import json

            cfg["extra"] = json.dumps(extra)
        elif extra is None:
            cfg["extra"] = "{}"

        ssh_tunnel_config = cfg.pop("ssh_tunnel", None)
        sqlalchemy_uri = cfg.pop("sqlalchemy_uri", "")

        # Trim non-model fields
        for key in ("version", "database_uuid", "uuid"):
            cfg.pop(key, None)

        attrs = {
            k: v
            for k, v in cfg.items()
            if k
            in {
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
        }
        attrs["sqlalchemy_uri"] = sqlalchemy_uri

        if existing:
            for key, value in attrs.items():
                setattr(existing, key, value)
            database = existing
        else:
            database = Database(**attrs)
            if uuid_str:
                database.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            self.session.add(database)

        await self.session.flush()

        if ssh_tunnel_config:
            await self._import_ssh_tunnel(int(database.id), dict(ssh_tunnel_config))

        return database

    async def _import_ssh_tunnel(
        self, database_id: int, config: dict[str, Any]
    ) -> None:
        from sqlalchemy import select

        try:
            from superset.models.ssh_tunnel import SSHTunnel
        except ImportError:
            return

        config["database_id"] = database_id
        config.pop("id", None)

        existing = (
            (
                await self.session.execute(
                    select(SSHTunnel).where(SSHTunnel.database_id == database_id)
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
                if key in config:
                    value = config[key]
                    # Don't overwrite secrets with mask placeholder.
                    if key in ("password", "private_key", "private_key_password") and (
                        value == "XXXXXXXXXX"
                    ):
                        continue
                    setattr(existing, key, value)
        else:
            filtered = {k: v for k, v in config.items() if k in attrs}
            self.session.add(SSHTunnel(**filtered))

        await self.session.flush()

    async def _import_dataset(self, config: dict[str, Any]) -> Any:
        """Upsert a SqlaTable + its columns/metrics via the async helper."""
        from superset.commands.chart.importers.v1.utils import _import_dataset

        return await _import_dataset(
            self.session,
            dict(config),
            overwrite=True,
            ignore_permissions=True,
            security_manager=self.security_manager,
            current_user=self.current_user,
        )

    async def _import_saved_query(self, config: dict[str, Any]) -> None:
        """Upsert a SavedQuery row via the canonical async helper."""
        from superset.commands.query.importers.v1.utils import import_saved_query

        await import_saved_query(self.session, dict(config), overwrite=True)

    # Note: ``migrate_dashboard`` is now invoked inline in :meth:`_import`
    # — no helper needed.


# Re-export the metadata file name for convenience (parity with original).
__all__ = ["METADATA_FILE_NAME", "ImportAssetsCommand"]
