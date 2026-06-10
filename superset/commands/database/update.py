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
"""Async port of ``superset_old/commands/database/update.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.database.utils import (
    _validate_extra,
    _validate_field_lengths,
    _validate_server_cert,
    _validate_sqlalchemy_uri_safety,
)
from superset.exceptions import (
    CommandInvalidError,
    OAuth2RedirectError,
    ObjectNotFoundError,
)

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.models.core import Database

logger = logging.getLogger(__name__)


class UpdateDatabaseCommand(AsyncBaseCommand["Database"]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._data = data
        self._user_id = user_id
        self._database: Any | None = None

    async def validate(self) -> None:
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

        new_name = self._data.get("database_name")
        if new_name:
            is_unique = await self._dao.validate_update_uniqueness(
                self._database_id,
                new_name,
            )
            if not is_unique:
                raise CommandInvalidError(
                    f'A database with the name "{new_name}" already exists'
                )

        # Field validators — 1:1 with ``DatabasePutSchema`` (extra/server_cert
        # validators + ``Length`` bounds: database_name 1-250, sqlalchemy_uri
        # 0-1024, force_ctas_schema 0-250) and ``sqlalchemy_uri_validator``
        # (make_url_safe + configurable ``check_sqlalchemy_uri`` when
        # ``PREVENT_UNSAFE_DB_CONNECTIONS`` is enabled). Each runs only when the
        # field is present in the (partial) PUT payload (PUT min length is 0).
        uri = self._data.get("sqlalchemy_uri")
        force_ctas_schema = self._data.get("force_ctas_schema")
        extra = self._data.get("extra")
        cert = self._data.get("server_cert")
        if isinstance(uri, str):
            _validate_sqlalchemy_uri_safety(uri)
        _validate_field_lengths(
            database_name=new_name if isinstance(new_name, str) else None,
            sqlalchemy_uri=uri if isinstance(uri, str) else None,
            force_ctas_schema=force_ctas_schema
            if isinstance(force_ctas_schema, str)
            else None,
            sqlalchemy_uri_min=0,
        )
        if isinstance(extra, str):
            _validate_extra(extra)
        if isinstance(cert, str):
            _validate_server_cert(cert)

    async def run(self) -> "Database":  # noqa: C901
        assert self._database is not None

        # Capture the original name before the setattr loop overwrites it, so a
        # rename can be propagated to the (name-based) FAB permissions below.
        original_database_name = self._database.database_name
        # Capture the original default catalog + multi-catalog flag BEFORE the
        # setattr loop mutates the model — used to propagate a default-catalog
        # change to dependent assets after the update (1:1 with upstream
        # ``UpdateDatabaseCommand._update_catalog_attribute``). For catalog-less
        # engines (e.g. postgres) ``get_default_catalog`` is ``None`` → no-op.
        original_catalog = self._database.get_default_catalog()
        original_allow_multi_catalog = bool(
            getattr(self._database, "allow_multi_catalog", False)
        )

        # --- build_sqlalchemy_uri --------------------------------------------
        # Mirrors the ``@pre_load`` hook on
        # ``DatabaseParametersSchemaMixin.build_sqlalchemy_uri`` from
        # ``superset_old.databases.schemas``: when the request uses
        # ``configuration_method == 'dynamic_form'`` the engine spec
        # composes the SQLAlchemy URI from ``{engine, driver, parameters,
        # masked_encrypted_extra}`` and the model only stores
        # ``sqlalchemy_uri``.  ``engine`` / ``driver`` / ``parameters``
        # are derived/read-only on ``Database`` (no setter) so they must
        # be popped here before the ``setattr`` loop below — otherwise
        # we'd raise ``AttributeError: property 'driver' has no setter``.
        configuration_method = self._data.get("configuration_method")
        engine = self._data.pop("engine", None)
        driver = self._data.pop("driver", None)
        # ``backend`` is a read-only property too — same treatment.
        self._data.pop("backend", None)
        parameters = self._data.pop("parameters", None)
        if configuration_method == "dynamic_form" and engine and parameters:
            try:
                from superset.db_engine_specs import get_engine_spec

                spec = get_engine_spec(engine, driver)
                if hasattr(spec, "build_sqlalchemy_uri") and hasattr(
                    spec, "parameters_schema"
                ):
                    masked_extra = (
                        self._data.get("masked_encrypted_extra")
                        or self._data.get("encrypted_extra")
                        or "{}"
                    )
                    import json as _json

                    try:
                        encrypted_extra = _json.loads(masked_extra)
                    except (TypeError, _json.JSONDecodeError):
                        encrypted_extra = {}
                    self._data["sqlalchemy_uri"] = spec.build_sqlalchemy_uri(
                        parameters,
                        encrypted_extra,
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to build sqlalchemy_uri from parameters",
                    exc_info=True,
                )

        # --- unmask_encrypted_extra ----------------------------------------
        # The PUT request may contain ``masked_encrypted_extra`` — a version of
        # ``encrypted_extra`` where sensitive fields (private keys, passwords,
        # etc.) are replaced with the "XXXXXXXXXX" sentinel by the
        # ``mask_encrypted_extra`` classmethod on the engine spec.
        #
        # Mirrors ``superset_old/commands/database/update.py`` lines 70-77:
        #   if "masked_encrypted_extra" in self._properties:
        #       self._properties["encrypted_extra"] = (
        #           self._model.db_engine_spec.unmask_encrypted_extra(
        #               self._model.encrypted_extra,
        #               self._properties.pop("masked_encrypted_extra"),
        #           )
        #       )
        #
        # Without this step the masked placeholders would be written verbatim
        # to the database, permanently destroying the real credentials.
        if "masked_encrypted_extra" in self._data:
            self._data["encrypted_extra"] = (
                self._database.db_engine_spec.unmask_encrypted_extra(
                    self._database.encrypted_extra,
                    self._data.pop("masked_encrypted_extra"),
                )
            )

        # ``hasattr`` matches read-only properties too (e.g. ``backend``
        # / ``driver``); fall through to ``setattr`` raises in that case.
        # The build_sqlalchemy_uri block above already popped the known
        # read-only keys, but we still guard each setattr with a setter
        # check so future schema additions can't crash this hot path.
        for key, value in self._data.items():
            attr = getattr(type(self._database), key, None)
            if isinstance(attr, property) and attr.fset is None:
                continue
            if hasattr(self._database, key):
                setattr(self._database, key, value)
        # Mirror ``superset_old.commands.database.update.UpdateDatabaseCommand.run``
        # line 97: ``database.set_sqlalchemy_uri(database.sqlalchemy_uri)``.
        # Required so a freshly-set URI containing the real password is
        # masked (PASSWORD_MASK in ``sqlalchemy_uri`` column, real
        # secret moved to the ``password`` column).
        if "sqlalchemy_uri" in self._data and hasattr(
            self._database, "set_sqlalchemy_uri"
        ):
            self._database.set_sqlalchemy_uri(self._database.sqlalchemy_uri)
        if self._user_id is not None:
            self._database.changed_by_fk = self._user_id
        await self._dao.session.flush()

        # Propagate a default-catalog change to dependent assets (SqlaTable /
        # Query / SavedQuery / TabState / TableSchema) — 1:1 with upstream:
        # only when the default catalog actually changed AND multi-catalog was
        # NOT enabled on either the old or new config (with multi-catalog the
        # per-asset catalog is meaningful and must be preserved).
        new_catalog = self._database.get_default_catalog()
        new_allow_multi_catalog = bool(
            getattr(self._database, "allow_multi_catalog", False)
        )
        if (
            new_catalog != original_catalog
            and not original_allow_multi_catalog
            and not new_allow_multi_catalog
        ):
            await self._update_catalog_attribute(self._database.id, new_catalog)

        # If the database name changed, existing permissions are name-based and
        # must be updated.  Mirrors superset_old UpdateDatabaseCommand which
        # always invokes SyncPermissionsCommand after an update (it internally
        # no-ops when old == new name).
        await self._sync_permissions(original_database_name)

        return self._database

    async def _update_catalog_attribute(
        self, database_id: int, new_catalog: str | None
    ) -> None:
        """Set ``catalog`` on all assets tied to this database — 1:1 with
        ``superset_old/commands/database/update.py::_update_catalog_attribute``.
        ``SavedQuery`` keys the database on ``db_id``; the others on
        ``database_id``."""
        from sqlalchemy import update as _sa_update

        from superset.models.connectors import SqlaTable
        from superset.models.sql_lab import Query, SavedQuery, TableSchema, TabState

        for model in (SqlaTable, Query, SavedQuery, TabState, TableSchema):
            fk = SavedQuery.db_id if model is SavedQuery else model.database_id
            await self._dao.session.execute(
                _sa_update(model).where(fk == database_id).values(catalog=new_catalog)
            )

    async def _sync_permissions(self, original_database_name: str) -> None:
        """Resync name-based catalog/schema permissions after an update.

        Swallows OAuth2 redirects (the connection needs re-auth) so the update
        itself never fails — mirrors the original's
        ``except (OAuth2RedirectError, MissingOAuth2TokenError): pass``.
        """
        from superset.commands.database.sync_permissions import (
            SyncPermissionsCommand,
        )
        from superset.config import SupersetSettings
        from superset.security.manager import build_async_security_manager
        from superset.utils.core import get_current_user

        try:
            security_manager = build_async_security_manager(
                self._dao.session,
                SupersetSettings(),  # type: ignore[call-arg]
            )
            current = get_current_user()
            username = getattr(current, "username", None)
            await SyncPermissionsCommand(
                dao=self._dao,
                database_id=self._database_id,
                security_manager=security_manager,
                username=username,
                old_db_connection_name=original_database_name,
                db_connection=self._database,
            ).execute()
        except OAuth2RedirectError:
            # The connection needs OAuth2 re-auth — don't fail the update.  Any
            # other error propagates (and rolls back the update), exactly as the
            # original's ``except (OAuth2RedirectError, MissingOAuth2TokenError)``
            # (``MissingOAuth2TokenError`` has no equivalent in the port — live
            # connection failures are already swallowed inside
            # ``SyncPermissionsCommand._get_catalog_names`` / ``_get_schema_names``).
            pass
