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
"""Command for importing database connections from a ZIP bundle."""

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
    _expected_type = "Database"

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

    async def _validate(  # noqa: C901
        self, configs: dict[str, dict[str, Any]]
    ) -> None:
        from uuid import UUID as _UUID

        from superset.commands.database.ssh_tunnel.exceptions import (
            SSHTunnelingNotEnabledError,
            SSHTunnelInvalidCredentials,
            SSHTunnelMissingCredentials,
        )
        from superset.databases.utils import make_url_safe
        from superset.utils.feature_flags import feature_flag_manager

        for name, config in configs.items():
            # A malformed YAML may parse to a list/scalar; guard before .get
            # so it is skipped rather than raising AttributeError → HTTP 500.
            if not (name.startswith("databases/") and isinstance(config, dict)):
                continue
            if not config.get("database_name"):
                raise CommandInvalidError(f"Missing database_name in {name}")

            # Validate credentials for NEW databases only; existing ones by UUID
            # keep their stored secrets.
            uuid_str = config.get("uuid")
            existing = None
            if uuid_str and self._dao is not None:
                try:
                    existing = await self._dao.find_one_or_none(
                        uuid=_UUID(str(uuid_str))
                    )
                except (ValueError, TypeError):
                    existing = None
            if existing:
                continue

            try:
                uri_password = make_url_safe(config.get("sqlalchemy_uri", "")).password
            except Exception:  # noqa: BLE001
                uri_password = None
            if uri_password == PASSWORD_MASK and config.get("password") is None:
                raise CommandInvalidError("Must provide a password for the database")

            ssh_tunnel = config.get("ssh_tunnel")
            if ssh_tunnel:
                if not feature_flag_manager.is_feature_enabled("SSH_TUNNELING"):
                    raise SSHTunnelingNotEnabledError()
                ssh_password = ssh_tunnel.get("password")
                private_key = ssh_tunnel.get("private_key")
                private_key_password = ssh_tunnel.get("private_key_password")
                if ssh_password is not None:
                    # Password auth must not mix with private-key auth.
                    if private_key is not None or private_key_password is not None:
                        raise SSHTunnelInvalidCredentials()
                    if ssh_password == PASSWORD_MASK:
                        raise CommandInvalidError(
                            "Must provide a password for the ssh tunnel"
                        )
                else:
                    if private_key is None and private_key_password is None:
                        raise SSHTunnelMissingCredentials()
                    msgs: list[str] = []
                    if private_key is None or private_key == PASSWORD_MASK:
                        msgs.append("Must provide a private key for the ssh tunnel")
                    if (
                        private_key_password is None
                        or private_key_password == PASSWORD_MASK
                    ):
                        msgs.append(
                            "Must provide a private key password for the ssh tunnel"
                        )
                    if msgs:
                        raise CommandInvalidError("; ".join(msgs))

    async def run(self) -> None:
        """Import databases first, then related datasets that reference them."""
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

        # overwrite=False for datasets so existing columns/metrics are preserved.
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
        """Import a single database config entry from the bundle."""
        if not file_name.startswith("databases/"):
            return
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        from uuid import UUID as _UUID

        from superset.models.core import Database

        config = dict(content)  # shallow copy

        # ``AsyncSecurityManager.can_access`` takes the user explicitly;
        # no user in context → deny (matches upstream behaviour).
        can_write = self._ignore_permissions
        if not can_write and self._security_manager is not None:
            if hasattr(self._security_manager, "can_access"):
                from superset.utils.core import get_current_user

                user = get_current_user()
                can_write = user is not None and (
                    await self._security_manager.can_access(
                        "can_write", "Database", user=user
                    )
                )
            else:
                can_write = True
        elif self._security_manager is None:
            can_write = True

        uuid_str = config.get("uuid")
        existing: Database | None = None
        if uuid_str:
            existing = await self._dao.find_one_or_none(uuid=_UUID(uuid_str))

        if existing:
            if not self._overwrite or not can_write:
                return
            config["id"] = existing.id
        elif not can_write:
            raise CommandInvalidError(
                "Database doesn't exist and user doesn't have permission "
                "to create databases"
            )

        if "allow_csv_upload" in config:
            config["allow_file_upload"] = config.pop("allow_csv_upload")

        extra = config.get("extra")
        if isinstance(extra, dict):
            if "schemas_allowed_for_csv_upload" in extra:
                extra["schemas_allowed_for_file_upload"] = extra.pop(
                    "schemas_allowed_for_csv_upload"
                )
            config["extra"] = json.dumps(extra)
        elif extra is None:
            config["extra"] = "{}"

        ssh_tunnel_config = config.pop("ssh_tunnel", None)
        sqlalchemy_uri = config.pop("sqlalchemy_uri", "")
        config.pop("version", None)
        config.pop("database_uuid", None)

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

        db_attrs["sqlalchemy_uri"] = sqlalchemy_uri

        if existing:
            for key, value in db_attrs.items():
                setattr(existing, key, value)
            if uuid_str:
                existing.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            database = existing
        else:
            database = Database(**db_attrs)
            if uuid_str:
                database.uuid = _UUID(uuid_str)  # type: ignore[assignment]
            self._dao.session.add(database)

        await self._dao.session.flush()

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
        config.pop("id", None)

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
                    if key in ("password", "private_key", "private_key_password"):
                        if value == PASSWORD_MASK:
                            continue
                    setattr(existing, key, value)
        else:
            filtered = {k: v for k, v in config.items() if k in tunnel_attrs}
            tunnel = SSHTunnel(**filtered)
            session.add(tunnel)

        await session.flush()
