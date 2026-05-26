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
"""Async port of ``superset_old/commands/database/validate_sql.py``.

1:1 with the original: dispatches to the per-engine
:class:`BaseSQLValidator` resolved via ``SQL_VALIDATORS_BY_ENGINE``
(``PostgreSQLValidator`` -> ``pgsanity``, ``PrestoDBSQLValidator`` ->
``EXPLAIN (TYPE VALIDATE)``).

When the engine has no validator configured the original raises
``NoValidatorConfigFoundError`` (HTTP 422); when the configured validator
name cannot be resolved it raises ``NoValidatorFoundError`` (HTTP 422).
There is **no** silent sqlglot fallback — that would mask the
"not configured" condition behind a 200 response, which is exactly the
behaviour this port restores.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CommandException,
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetErrorException,
)
from superset.i18n import gettext as __

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.sql.validators.base import BaseSQLValidator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validator exceptions — ported 1:1 from
# ``superset_old/commands/database/exceptions.py``.  They are defined here
# (rather than in ``commands/database/exceptions.py``) so this port stays
# self-contained; the status codes match the original exactly.
# ---------------------------------------------------------------------------


class NoValidatorConfigFoundError(SupersetErrorException):
    """No SQL validator configured for the database's engine (HTTP 422)."""

    status_code = 422


class NoValidatorFoundError(SupersetErrorException):
    """The configured validator name could not be resolved (HTTP 422)."""

    status_code = 422


class ValidatorSQLError(SupersetErrorException):
    """The validator itself failed to check the query (HTTP 422)."""

    status_code = 422


class ValidatorSQL400Error(SupersetErrorException):
    """The validator failed with a 4xx-style database error (HTTP 400)."""

    status_code = 400


class ValidatorSQLUnexpectedError(CommandException):
    """Validator/model never populated — should never happen (HTTP 422)."""

    status_code = 422
    message = __("An unexpected error occurred")


class ValidateSQLCommand(AsyncBaseCommand[list[dict[str, Any]]]):
    """Validate a SQL statement against the database engine's validator.

    1:1 port of ``superset_old/commands/database/validate_sql.py``:

    1. Load the database row (404 when missing).
    2. Resolve the validator name via ``SQL_VALIDATORS_BY_ENGINE``;
       raise :class:`NoValidatorConfigFoundError` (422) when the engine
       has no validator configured.
    3. Resolve the validator class by name; raise
       :class:`NoValidatorFoundError` (422) when the name is unknown.
    4. Run the (synchronous) validator inside :func:`asyncio.to_thread`,
       wrapped in the configured validation timeout. Wrap failures in
       :class:`ValidatorSQL400Error` / :class:`ValidatorSQLError`.
    """

    def __init__(
        self,
        dao: "AsyncDatabaseDAO",
        database_id: int,
        sql: str,
        schema: str | None = None,
        catalog: str | None = None,
        template_params: dict[str, Any] | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._sql = sql
        self._schema = schema
        self._catalog = catalog
        self._template_params = template_params or {}
        self._database: Any | None = None
        self._validator: type[BaseSQLValidator] | None = None

    async def validate(self) -> None:
        # Validate/populate model exists — matches the original
        # ``ValidateSQLCommand.validate`` which raises ``DatabaseNotFoundError``
        # (404) before any validator lookup.
        if not self._sql or not self._sql.strip():
            raise CommandInvalidError("SQL query is required")
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

        spec = self._database.db_engine_spec
        validators_by_engine = self._validators_by_engine()
        engine = getattr(spec, "engine", None)
        if not validators_by_engine or engine not in validators_by_engine:
            raise NoValidatorConfigFoundError(
                SupersetError(
                    message=__(
                        "no SQL validator is configured for %(engine_spec)s",
                        engine_spec=engine,
                    ),
                    error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
                    level=ErrorLevel.ERROR,
                ),
            )
        validator_name = validators_by_engine[engine]

        from superset.sql.validators import get_validator_by_name

        self._validator = get_validator_by_name(validator_name)
        if not self._validator:
            raise NoValidatorFoundError(
                SupersetError(
                    message=__(
                        "No validator named %(validator_name)s found "
                        "(configured for the %(engine_spec)s engine)",
                        validator_name=validator_name,
                        engine_spec=engine,
                    ),
                    error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
                    level=ErrorLevel.ERROR,
                ),
            )

    async def run(self) -> list[dict[str, Any]]:
        """Validate the SQL statement and return a list of annotations.

        :return: A list of ``SQLValidationAnnotation`` dicts.
        :raises: ObjectNotFoundError, NoValidatorConfigFoundError,
          NoValidatorFoundError, ValidatorSQLUnexpectedError,
          ValidatorSQLError, ValidatorSQL400Error
        """
        if self._database is None or self._validator is None:
            await self.validate()
        if not self._validator or not self._database:
            raise ValidatorSQLUnexpectedError()
        try:
            errors = await asyncio.to_thread(self._validate_sync)
            return [err.to_dict() for err in errors]
        except Exception as ex:  # noqa: BLE001
            logger.exception("SQL validation failed")
            superset_error = SupersetError(
                message=__(
                    "%(validator)s was unable to check your query.\n"
                    "Please recheck your query.\n"
                    "Exception: %(ex)s",
                    validator=self._validator.name,
                    ex=ex,
                ),
                error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
                level=ErrorLevel.ERROR,
            )

            # Return as a 400 if the database error message says we got a
            # 4xx error — matches the original regex.
            if re.search(r"([\W]|^)4\d{2}([\W]|$)", str(ex)):
                raise ValidatorSQL400Error(superset_error) from ex
            raise ValidatorSQLError(superset_error) from ex

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _validate_sync(self) -> list[Any]:
        """Run the (synchronous) validator within the validation timeout.

        Mirrors the original ``with utils.timeout(...)`` guard around
        ``self._validator.validate(sql, catalog, schema, self._model)``.
        Runs in a worker thread because the validators are fully
        synchronous (``pgsanity`` / ``EXPLAIN`` over a sync engine).

        ``SigalrmTimeout`` (the port of ``utils.timeout``) self-disables
        when not on the main thread — matching the original's defensive
        "timeout can't be used in the current context" branch — so the
        guard is a safe no-op inside the worker thread.
        """
        from superset.utils.core import SigalrmTimeout

        seconds = self._validation_timeout()
        timeout_msg = f"The query exceeded the {seconds} seconds timeout."
        with SigalrmTimeout(seconds=seconds, error_message=timeout_msg):
            return self._validator.validate(
                self._sql,
                self._catalog,
                self._schema,
                self._database,
            )

    def _validators_by_engine(self) -> dict[str, str]:
        """Return the ``SQL_VALIDATORS_BY_ENGINE`` config map.

        Mirrors ``app.config["SQL_VALIDATORS_BY_ENGINE"]`` from the
        original; exposed on
        :attr:`SupersetSettings.sql_validators_by_engine`.
        """
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            return getattr(settings, "sql_validators_by_engine", {}) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _validation_timeout(self) -> int:
        """Return ``SQLLAB_VALIDATION_TIMEOUT`` (seconds)."""
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            return int(getattr(settings, "sqllab_validation_timeout", 10))
        except Exception:  # noqa: BLE001
            return 10
