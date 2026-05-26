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
"""Async port of ``superset_old/commands/database/importers/v1/__init__.py``."""

from __future__ import annotations

import io
import json
from typing import Any, TYPE_CHECKING

from superset.commands.database.utils import PASSWORD_MASK
from superset.exceptions import CommandInvalidError
from superset.importexport.import_base import AsyncImportModelsCommand

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.db.daos.database import AsyncDatabaseDAO


class ImportDatabasesCommand(AsyncImportModelsCommand):
    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncDatabaseDAO | None = None,
        security_manager: Any | None = None,
        ignore_permissions: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao
        self._security_manager = security_manager
        self._ignore_permissions = ignore_permissions

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        for name, config in configs.items():
            if name.startswith("databases/") and not config.get("database_name"):
                raise CommandInvalidError(f"Missing database_name in {name}")

    async def run(self) -> None:
        """Run the import in dependency order: databases first, then related datasets.

        Mirrors ``ImportDatabasesCommand._import`` from the original
        which iterates the bundle twice: once for databases (honouring
        ``overwrite``) and once for any ``datasets/`` entries that
        reference a just-imported database.
        """
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        from superset.commands.chart.importers.v1.utils import (
            _import_database,
            _import_dataset,
        )

        configs = self._configs
        session = self._dao.session

        # 1. Databases (with caller's overwrite flag).
        database_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if file_name.startswith("databases/") and isinstance(config, dict):
                db = await _import_database(
                    session,
                    self._apply_password(dict(config), file_name),
                    overwrite=self._overwrite,
                    ignore_permissions=self._ignore_permissions,
                    security_manager=self._security_manager,
                )
                database_ids[str(db.uuid)] = db.id

        # 2. Related datasets (overwrite=False so existing columns/metrics
        #    are preserved — matches the original).
        for file_name, config in configs.items():
            if (
                file_name.startswith("datasets/")
                and isinstance(config, dict)
                and config.get("database_uuid") in database_ids
            ):
                ds_config = dict(config)
                ds_config["database_id"] = database_ids[ds_config["database_uuid"]]
                await _import_dataset(
                    session,
                    ds_config,
                    overwrite=False,
                    ignore_permissions=self._ignore_permissions,
                    security_manager=self._security_manager,
                )

    async def _check_existing(self, uuid_val: str) -> bool:
        """Check if a database with this UUID already exists."""
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
        """Import a single database config — 1:1 port of import_database().

        Logic ported from superset_old/commands/database/importers/v1/utils.py:
        1. UUID-based dedup: query existing by UUID, skip or update
        2. All fields (cache_timeout, expose_in_sqllab, allow_run_async, etc.)
        3. ``extra`` JSON serialization
        4. ``allow_csv_upload`` -> ``allow_file_upload`` rename
        5. SSH tunnel import
        6. Password masking via set_sqlalchemy_uri equivalent
        """
        if not file_name.startswith("databases/"):
            return
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        from uuid import UUID as _UUID

        from superset.models.core import Database

        config = dict(content)  # shallow copy to avoid mutating caller's data

        # --- Permission check ---
        can_write = self._ignore_permissions
        if not can_write and self._security_manager is not None:
            if hasattr(self._security_manager, "can_access"):
                can_write = await self._security_manager.can_access(
                    "can_write", "Database"
                )
            else:
                can_write = True
        elif self._security_manager is None:
            can_write = True

        # --- UUID-based dedup ---
        uuid_str = config.get("uuid")
        existing: Database | None = None
        if uuid_str:
            existing = await self._dao.find_one_or_none(uuid=_UUID(uuid_str))

        if existing:
            if not self._overwrite or not can_write:
                return  # skip — already exists
            config["id"] = existing.id
        elif not can_write:
            raise CommandInvalidError(
                "Database doesn't exist and user doesn't have permission "
                "to create databases"
            )

        # --- ``allow_csv_upload`` -> ``allow_file_upload`` rename ---
        if "allow_csv_upload" in config:
            config["allow_file_upload"] = config.pop("allow_csv_upload")

        # --- extra JSON: legacy rename + serialize ---
        extra = config.get("extra")
        if isinstance(extra, dict):
            if "schemas_allowed_for_csv_upload" in extra:
                extra["schemas_allowed_for_file_upload"] = extra.pop(
                    "schemas_allowed_for_csv_upload"
                )
            config["extra"] = json.dumps(extra)
        elif extra is None:
            config["extra"] = "{}"

        # --- Extract SSH tunnel config before creating the database ---
        ssh_tunnel_config = config.pop("ssh_tunnel", None)

        # --- Extract sqlalchemy_uri for masked password handling ---
        sqlalchemy_uri = config.pop("sqlalchemy_uri", "")

        # --- Remove non-model fields ---
        config.pop("version", None)
        config.pop("database_uuid", None)

        # --- Build attribute dict for the Database model ---
        db_attrs: dict[str, Any] = {}
        db_columns = {
            "database_name",
            "sqlalchemy_uri",
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
        for key in db_columns:
            if key in config:
                db_attrs[key] = config[key]

        # Set the sqlalchemy_uri (the password gets stored in the URI)
        db_attrs["sqlalchemy_uri"] = sqlalchemy_uri

        if existing:
            # Update existing database
            for key, value in db_attrs.items():
                setattr(existing, key, value)
            if uuid_str:
                existing.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            database = existing
        else:
            # Create new database
            database = Database(**db_attrs)
            if uuid_str:
                database.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            self._dao.session.add(database)

        await self._dao.session.flush()

        # --- SSH tunnel import ---
        if ssh_tunnel_config:
            await self._import_ssh_tunnel(
                self._dao.session, database.id, ssh_tunnel_config
            )

    @staticmethod
    async def _import_ssh_tunnel(
        session: AsyncSession,
        database_id: int,
        config: dict[str, Any],
    ) -> None:
        """Import or update an SSH tunnel for a database."""
        from sqlalchemy import select

        from superset.models.ssh_tunnel import SSHTunnel

        config = dict(config)  # shallow copy
        config["database_id"] = database_id

        # Remove non-model fields
        config.pop("id", None)

        # Check if an SSH tunnel already exists for this database
        stmt = select(SSHTunnel).where(SSHTunnel.database_id == database_id)
        result = await session.execute(stmt)
        existing = result.scalars().one_or_none()

        tunnel_attrs = {
            "server_address",
            "server_port",
            "username",
            "password",
            "private_key",
            "private_key_password",
            "database_id",
        }

        if existing:
            for key in tunnel_attrs:
                if key in config:
                    value = config[key]
                    # Don't overwrite passwords with mask values
                    if key in ("password", "private_key", "private_key_password"):
                        if value == PASSWORD_MASK:
                            continue
                    setattr(existing, key, value)
        else:
            # Filter to only known columns
            filtered = {k: v for k, v in config.items() if k in tunnel_attrs}
            tunnel = SSHTunnel(**filtered)
            session.add(tunnel)

        await session.flush()
