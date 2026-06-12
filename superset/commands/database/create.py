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
"""Async port of ``superset_old/commands/database/create.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.database.exceptions import DatabaseParametersInvalidError
from superset.commands.database.test_connection import DatabaseTestConnectionCommand
from superset.commands.database.utils import (
    _validate_extra,
    _validate_field_lengths,
    _validate_server_cert,
    _validate_sqlalchemy_uri_safety,
)
from superset.exceptions import CommandInvalidError

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.models.core import Database

logger = logging.getLogger(__name__)


class CreateDatabaseCommand(AsyncBaseCommand["Database"]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id

    async def validate(self) -> None:  # noqa: C901  # complex business logic
        if not self._data.get("database_name"):
            raise CommandInvalidError("database_name is required")

        # Build sqlalchemy_uri from parameters when using dynamic_form,
        # matching original Marshmallow pre_load at
        # superset_old/databases/schemas.py:304-363.
        #
        # Like the original, we MUST pop `parameters`, `engine`, and
        # `driver` from data — they are not columns on the Database model
        # and `setattr`/`Model(**data)` will fail on them (e.g. `driver`
        # is a read-only @property on Database).
        parameters = self._data.pop("parameters", {}) or {}
        engine = (
            self._data.pop("engine", None)
            or (
                parameters.pop("engine", None) if isinstance(parameters, dict) else None
            )
            or self._data.pop("backend", None)
        )
        driver = self._data.pop("driver", None)

        if (
            not self._data.get("sqlalchemy_uri")
            and self._data.get("configuration_method") == "dynamic_form"
        ):
            if not engine:
                raise CommandInvalidError(
                    "An engine must be specified when passing individual "
                    "parameters to a database."
                )
            from superset.db_engine_specs import get_engine_spec

            spec_class = get_engine_spec(engine, driver)
            if not hasattr(spec_class, "build_sqlalchemy_uri") or not hasattr(
                spec_class, "parameters_schema"
            ):
                raise CommandInvalidError(
                    f'Engine spec "{engine}" does not support being '
                    "configured via individual parameters."
                )

            import json as _json

            encrypted_extra_str = self._data.get("masked_encrypted_extra") or "{}"
            try:
                encrypted_extra = _json.loads(encrypted_extra_str)
            except (ValueError, TypeError):
                encrypted_extra = {}

            try:
                self._data["sqlalchemy_uri"] = spec_class.build_sqlalchemy_uri(
                    parameters,
                    encrypted_extra,
                )
            except ValueError as ex:
                # Engine specs (e.g. BigQuery) raise ValueError for missing /
                # invalid credentials — surface as a structured 422 instead of
                # propagating to the generic 500 handler.
                # Mirrors original: marshmallow @pre_load hook raised
                # marshmallow.ValidationError → caught by Schema.load() →
                # API handler returned response_400.
                raise DatabaseParametersInvalidError(str(ex)) from ex

        if not self._data.get("sqlalchemy_uri"):
            raise CommandInvalidError("sqlalchemy_uri is required")

        # Validate URI safety — 1:1 with the original
        # ``sqlalchemy_uri_validator`` (superset_old/databases/schemas.py:196-216):
        # parse the URI via ``make_url_safe`` and, when
        # ``PREVENT_UNSAFE_DB_CONNECTIONS`` is enabled (the default), reject
        # blocklisted dialects (sqlite/shillelagh/meta-DB) through the
        # configurable ``check_sqlalchemy_uri`` allowlist rather than a
        # hardcoded ``{file, sqlite}`` set.
        _validate_sqlalchemy_uri_safety(self._data.get("sqlalchemy_uri", ""))

        # Field validators — 1:1 with ``DatabasePostSchema`` (extra/server_cert
        # validators + ``Length`` bounds: database_name 1-250, sqlalchemy_uri
        # 1-1024, force_ctas_schema 0-250).
        _validate_field_lengths(
            database_name=self._data.get("database_name"),
            sqlalchemy_uri=self._data.get("sqlalchemy_uri"),
            force_ctas_schema=self._data.get("force_ctas_schema"),
            sqlalchemy_uri_min=1,
        )
        _validate_extra(self._data.get("extra"))
        _validate_server_cert(self._data.get("server_cert"))

        is_unique = await self._dao.validate_uniqueness(
            self._data["database_name"],
        )
        if not is_unique:
            # Field-keyed 422 — 1:1 with upstream
            # ``DatabaseInvalidError(exceptions=[DatabaseExistsValidationError()])``
            # → ``{"database_name": ["A database with the same name already
            # exists."]}``.
            from superset.commands.database.exceptions import (
                DatabaseExistsValidationError,
                DatabaseInvalidError,
            )
            from superset.events import event_logger

            exception = DatabaseInvalidError(
                exceptions=[DatabaseExistsValidationError()]
            )
            # Analytics event — 1:1 with upstream create.py:146-152.
            event_logger.log_with_context(
                action="db_connection_failed.{}.{}".format(
                    exception.__class__.__name__,
                    ".".join(exception.get_list_classnames()),
                )
            )
            raise exception

    def _log_creation_failed(self, ex: Exception, suffix: str = "") -> None:
        """Emit the ``db_creation_failed.<ExcCls>[suffix]`` analytics event.

        1:1 with ``superset_old/commands/database/create.py`` which logs
        ``event_logger.log_with_context(action=f"db_creation_failed.
        {ex.__class__.__name__}", engine=uri.split(":")[0])`` on every failed
        creation path.
        """
        from superset.events import event_logger

        event_logger.log_with_context(
            action=f"db_creation_failed.{ex.__class__.__name__}{suffix}",
            engine=(self._data.get("sqlalchemy_uri") or "").split(":")[0],
        )

    async def run(self) -> "Database":
        from superset.commands.database.ssh_tunnel.exceptions import (
            SSHTunnelDatabasePortError,
            SSHTunnelingNotEnabledError,
        )
        from superset.exceptions import (
            DatabaseConnectionFailedError,
            OAuth2RedirectError,
            SupersetErrorsException,
        )

        # -------------------------------------------------------------
        # Test connection BEFORE creating the database record.
        #
        # Matches original CreateDatabaseCommand.run() at
        # superset_old/commands/database/create.py:58-86 — the test
        # runs BEFORE self._create_database() so that a failed
        # connection aborts creation entirely.
        #
        # - OAuth2RedirectError is allowed (creation proceeds anyway)
        # - SupersetErrorsException is re-raised with its original
        #   SIP-40 error payload so the frontend can show actionable
        #   CONNECTION_* errors
        # - SSHTunnelingNotEnabledError (400) and SSHTunnelDatabasePortError
        #   (422) are re-raised unchanged so the frontend sees the correct
        #   status code and message — mirrors original:
        #   superset_old/commands/database/create.py:70-80.
        #   Neither extends SupersetErrorsException, so without this clause
        #   they would fall to the generic handler and become HTTP 500.
        # - Any other exception is wrapped in DatabaseConnectionFailedError
        # -------------------------------------------------------------
        try:
            test_cmd = DatabaseTestConnectionCommand(
                dao=self._dao,
                data=dict(self._data),
                user_id=self._user_id,
            )
            await test_cmd.validate()
            await test_cmd.run()
        except OAuth2RedirectError:
            # If we can't connect to the database due to an OAuth2 error
            # we can still save the database. Later, the user can sync
            # permissions when setting up data access rules.  Mirrors
            # ``superset_old/commands/database/create.py:65-69``.
            pass
        except (
            SupersetErrorsException,
            SSHTunnelingNotEnabledError,
            SSHTunnelDatabasePortError,
        ) as ex:
            # Re-raise so the engine-spec-extracted errors and SSH errors
            # reach the client with the correct status code.  Analytics
            # event — 1:1 with upstream create.py:75-80.
            self._log_creation_failed(ex)
            raise
        except Exception as ex:
            # Analytics event — 1:1 with upstream create.py:81-86.
            self._log_creation_failed(ex)
            raise DatabaseConnectionFailedError() from ex

        # -------------------------------------------------------------
        # Connection test succeeded — proceed to create the record.
        # -------------------------------------------------------------
        data = dict(self._data)

        # Rename masked_encrypted_extra → encrypted_extra on create:
        # when creating a new database we don't need to unmask.
        # Matches original _create_database at
        # superset_old/commands/database/create.py:155-163.
        if "masked_encrypted_extra" in data:
            data["encrypted_extra"] = data.pop("masked_encrypted_extra", "{}")

        # Filter to only fields the Database model actually accepts
        # (matches Marshmallow `unknown = EXCLUDE` in DatabasePostSchema).
        # The frontend POST body includes fields like engine_information,
        # sqlalchemy_uri_placeholder, ssh_tunnel, etc. that must not be
        # passed to Database().
        from sqlalchemy.inspection import inspect as sa_inspect

        from superset.models.core import Database

        allowed_cols = {c.key for c in sa_inspect(Database).mapper.column_attrs}
        # FK override fields we set below are also allowed
        allowed_cols |= {"created_by_fk", "changed_by_fk"}
        data = {k: v for k, v in data.items() if k in allowed_cols}

        if self._user_id is not None:
            data["created_by_fk"] = self._user_id
            data["changed_by_fk"] = self._user_id
        db = await self._dao.create(data)

        # Split the cleartext password out of ``sqlalchemy_uri`` into the
        # encrypted ``password`` column and store a masked URI — exactly like
        # the original ``_create_database`` at
        # superset_old/commands/database/create.py:162-164:
        #
        #     database = DatabaseDAO.create(attributes=self._properties)
        #     database.set_sqlalchemy_uri(database.sqlalchemy_uri)
        #
        # ``set_sqlalchemy_uri`` (superset/models/core.py) is a pure-Python
        # sync model method — it only parses/rewrites the URL string and sets
        # ``self.password`` / ``self.sqlalchemy_uri`` attributes, performing no
        # DB I/O — so it is safe to call directly (no await) on the transient
        # instance before the flush below persists the masked values.
        db.set_sqlalchemy_uri(db.sqlalchemy_uri)

        await self._dao.session.flush()
        return db
