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
"""Command for updating a database connection."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.database.exceptions import MissingOAuth2TokenError
from superset.commands.database.utils import (
    _validate_extra,
    _validate_field_lengths,
    _validate_server_cert,
    _validate_sqlalchemy_uri_safety,
)
from superset.exceptions import (
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
                from superset.commands.database.exceptions import (
                    DatabaseExistsValidationError,
                    DatabaseInvalidError,
                )

                raise DatabaseInvalidError(exceptions=[DatabaseExistsValidationError()])

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

        original_database_name = self._database.database_name
        # Capture the original default catalog before the setattr loop mutates
        # the model; a broken connection raises here — swallow and force the
        # asset update so the connection can still be fixed via PUT.
        force_update = False
        try:
            original_catalog = self._database.get_default_catalog()
        except Exception:  # noqa: BLE001
            original_catalog = None
            force_update = True

        # ``engine`` / ``driver`` / ``backend`` are read-only properties on
        # ``Database`` (no setter); pop them before the setattr loop.
        configuration_method = self._data.get("configuration_method")
        engine = self._data.pop("engine", None)
        driver = self._data.pop("driver", None)
        self._data.pop("backend", None)
        parameters = self._data.pop("parameters", None)
        if configuration_method == "dynamic_form" and parameters:
            # Build failure must not silently save the old URI → HTTP 400.
            from superset.commands.database.exceptions import (
                DatabaseParametersInvalidError,
            )
            from superset.db_engine_specs import get_engine_spec

            if not engine:
                raise DatabaseParametersInvalidError(
                    "An engine must be specified when passing individual "
                    "parameters to a database."
                )
            spec = get_engine_spec(engine, driver)
            if not hasattr(spec, "build_sqlalchemy_uri") or not hasattr(
                spec, "parameters_schema"
            ):
                raise DatabaseParametersInvalidError(
                    f'Engine spec "{engine}" does not support being '
                    "configured via individual parameters."
                )
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
            try:
                self._data["sqlalchemy_uri"] = spec.build_sqlalchemy_uri(
                    parameters,
                    encrypted_extra,
                )
            except ValueError as ex:
                raise DatabaseParametersInvalidError(str(ex)) from ex

            # ``validate()`` checked the *incoming* URI, but this rebuilds it
            # from ``parameters`` afterwards and persists the result — so the
            # blocklist has to be re-applied to the value that actually lands
            # in the database, not only to the one the caller sent.
            _validate_sqlalchemy_uri_safety(self._data["sqlalchemy_uri"])

        # Unmask before writing: without this step the "XXXXXXXXXX" sentinel
        # would be persisted verbatim, permanently destroying stored credentials.
        if "masked_encrypted_extra" in self._data:
            self._data["encrypted_extra"] = (
                self._database.db_engine_spec.unmask_encrypted_extra(
                    self._database.encrypted_extra,
                    self._data.pop("masked_encrypted_extra"),
                )
            )

        # Guard each setattr against read-only properties; the known ones were
        # popped above, but future schema additions might add more.
        for key, value in self._data.items():
            attr = getattr(type(self._database), key, None)
            if isinstance(attr, property) and attr.fset is None:
                continue
            if hasattr(self._database, key):
                setattr(self._database, key, value)
        # Split real password into ``password`` column and store masked URI.
        if "sqlalchemy_uri" in self._data and hasattr(
            self._database, "set_sqlalchemy_uri"
        ):
            self._database.set_sqlalchemy_uri(self._database.sqlalchemy_uri)
        if self._user_id is not None:
            self._database.changed_by_fk = self._user_id
        await self._dao.session.flush()

        new_catalog = self._database.get_default_catalog()
        new_allow_multi_catalog = bool(
            getattr(self._database, "allow_multi_catalog", False)
        )
        if force_update or (
            new_catalog != original_catalog and not new_allow_multi_catalog
        ):
            await self._update_catalog_attribute(self._database.id, new_catalog)

        await self._sync_permissions(original_database_name)

        return self._database

    async def _update_catalog_attribute(
        self, database_id: int, new_catalog: str | None
    ) -> None:
        """Set ``catalog`` on all assets for this database.

        ``SavedQuery`` uses ``db_id``; all other models use ``database_id``.
        """
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
        itself never fails — catches ``OAuth2RedirectError`` and
        ``MissingOAuth2TokenError`` silently.
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
        except (OAuth2RedirectError, MissingOAuth2TokenError):
            # MissingOAuth2TokenError: SyncPermissionsCommand.validate() pings
            # the DB; an OAuth2-needing DB would 500 without this catch.
            pass
