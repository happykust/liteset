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
"""Database command classes — business logic for database CRUD and operations."""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any, TYPE_CHECKING

import pandas as pd
import yaml  # type: ignore[import-untyped]

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
)
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.importexport.import_base import AsyncImportModelsCommand
from superset.utils import mask_uri_password

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.models.core import Database

EXPORT_VERSION = "1.0.0"

# Regex to sanitize file names (remove unsafe characters)
_SAFE_FILENAME_RE = re.compile(r"[^\w\s\-.]")

PASSWORD_MASK = "XXXXXXXXXX"  # noqa: S105


def _safe_filename(name: str) -> str:
    """Create a safe filename from a model name (like werkzeug's secure_filename)."""
    name = _SAFE_FILENAME_RE.sub("", name).strip()
    return name or "unnamed"


def _parse_extra(extra_payload: str) -> dict[str, Any]:
    """Parse the extra JSON field from a Database, with legacy fixups."""
    try:
        extra = json.loads(extra_payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    # Fix for DBs saved with an invalid ``schemas_allowed_for_csv_upload``
    schemas_allowed = extra.get("schemas_allowed_for_csv_upload")
    if isinstance(schemas_allowed, str):
        try:
            extra["schemas_allowed_for_csv_upload"] = json.loads(schemas_allowed)
        except (json.JSONDecodeError, TypeError):
            pass
    return extra


def _mask_ssh_tunnel_passwords(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask password fields in an SSH tunnel export payload."""
    masked = dict(payload)
    for key in ("password", "private_key", "private_key_password"):
        if masked.get(key):
            masked[key] = PASSWORD_MASK
    return masked


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

    async def validate(self) -> None:
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
            or (parameters.pop("engine", None) if isinstance(parameters, dict) else None)
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

            self._data["sqlalchemy_uri"] = spec_class.build_sqlalchemy_uri(
                parameters,
                encrypted_extra,
            )

        if not self._data.get("sqlalchemy_uri"):
            raise CommandInvalidError("sqlalchemy_uri is required")

        # Validate URI scheme safety
        uri = self._data.get("sqlalchemy_uri", "")
        if uri:
            from urllib.parse import urlparse

            parsed = urlparse(uri)
            if not parsed.scheme:
                raise CommandInvalidError("Invalid database URI: missing scheme")

            # Check for unsafe schemes
            UNSAFE_SCHEMES = {"file", "sqlite"}  # noqa: N806
            if parsed.scheme.lower().split("+")[0] in UNSAFE_SCHEMES:
                raise CommandInvalidError(
                    f"Database URI scheme '{parsed.scheme}' is not allowed"
                )

        is_unique = await self._dao.validate_uniqueness(
            self._data["database_name"],
        )
        if not is_unique:
            raise CommandInvalidError(
                f'Database "{self._data["database_name"]}" already exists'
            )

    async def run(self) -> "Database":
        from superset.exceptions import (
            DatabaseConnectionFailedError,
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
        # - Any other exception is wrapped in DatabaseConnectionFailedError
        # -------------------------------------------------------------
        try:
            test_cmd = DatabaseTestConnectionCommand(
                dao=self._dao,
                data=dict(self._data),
            )
            await test_cmd.validate()
            await test_cmd.run()
        except SupersetErrorsException:
            # Re-raise so the engine-spec-extracted errors reach the client
            raise
        except Exception as ex:
            # OAuth2 not yet implemented in liteset; treat every other
            # exception as a hard connection failure.
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
        await self._dao.session.flush()
        return db


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

    async def run(self) -> "Database":
        assert self._database is not None
        for key, value in self._data.items():
            if hasattr(self._database, key):
                setattr(self._database, key, value)
        if self._user_id is not None:
            self._database.changed_by_fk = self._user_id
        await self._dao.session.flush()
        return self._database


class DeleteDatabaseCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._security_manager = security_manager
        self._user_id = user_id
        self._database: Any | None = None

    async def validate(self) -> None:
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._database, self._user_id
            )
        has_datasets = False
        try:
            from superset.models.connectors import SqlaTable
        except (ImportError, ModuleNotFoundError):
            SqlaTable = None  # type: ignore[assignment,misc]  # noqa: N806
        if SqlaTable is not None:
            from sqlalchemy import func, select

            count = await self._dao.session.scalar(
                select(func.count()).where(SqlaTable.database_id == self._database_id)
            )
            if count and count > 0:
                has_datasets = True
        elif hasattr(self._dao, "has_dependent_datasets"):
            has_datasets = await self._dao.has_dependent_datasets(self._database_id)
        if has_datasets:
            raise CommandInvalidError(
                "Cannot delete database: dependent datasets exist"
            )
        if hasattr(self._dao, "find_report_schedules_by_database_id"):
            reports = await self._dao.find_report_schedules_by_database_id(
                self._database_id
            )
            if reports:
                raise CommandInvalidError(
                    "Cannot delete: associated report schedules exist"
                )

    async def run(self) -> None:
        assert self._database is not None
        await self._dao.session.delete(self._database)
        await self._dao.session.flush()


class DatabaseTestConnectionCommand(AsyncBaseCommand[dict[str, Any]]):
    """Test database connectivity.

    Ported 1:1 from superset_old/commands/database/test_connection.py.
    Builds an ephemeral Database model from the payload, resolves
    the URI (including existing model URI decryption for masked URIs),
    and opens an async connection to verify reachability.
    """

    __test__ = False  # prevent pytest collection

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        data: dict[str, Any],
    ) -> None:
        self._dao = dao
        self._data = data
        self._model: Database | None = None

    async def validate(self) -> None:
        uri = self._data.get("sqlalchemy_uri")
        if not uri:
            raise CommandInvalidError("sqlalchemy_uri is required for connection test")

        # If a database_name is provided, try to load the existing model
        # so we can decrypt a masked URI back to the real one.
        database_name = self._data.get("database_name")
        if database_name:
            self._model = await self._dao.get_database_by_name(database_name)

    async def run(self) -> dict[str, Any]:  # noqa: C901
        from sqlalchemy.exc import DBAPIError, NoSuchModuleError

        from superset.databases.utils import make_url_safe
        from superset.exceptions import (
            DatabaseTestConnectionDriverError,
            DatabaseTestConnectionUnexpectedError,
            SupersetErrorsException,
        )
        from superset.utils.database import get_async_connection

        uri = self._data.get("sqlalchemy_uri", "")

        # If the URI matches the masked version of an existing model,
        # use the decrypted URI from the model instead.
        if self._model:
            safe_uri = mask_uri_password(str(self._model.sqlalchemy_uri))
            if uri == safe_uri:
                uri = str(self._model.sqlalchemy_uri)

        # Parse URL into pieces for error context (hostname, port, etc.).
        # Used by engine_spec.extract_errors() to produce SIP-40 error
        # responses.  Matches superset_old/commands/database/test_connection.py:79-89
        url = make_url_safe(uri)
        context = {
            "hostname": url.host,
            "password": url.password,
            "port": url.port,
            "username": url.username,
            "database": url.database,
        }

        # Build an ephemeral Database model for the connection test
        database = self._dao.build_db_for_connection_test(
            server_cert=self._data.get("server_cert", ""),
            extra=self._data.get("extra", "{}"),
            impersonate_user=self._data.get("impersonate_user", False),
            encrypted_extra=self._data.get("masked_encrypted_extra", "{}"),
        )
        database.sqlalchemy_uri = uri

        try:
            async with get_async_connection(database) as (conn, engine_spec):
                # Run a simple ``SELECT 1`` to verify connectivity
                from sqlalchemy import text

                await conn.execute(text("SELECT 1"))

            return {"message": "OK"}

        except (NoSuchModuleError, ModuleNotFoundError) as ex:
            raise DatabaseTestConnectionDriverError(
                message=(
                    f"Could not load database driver: "
                    f"{database.db_engine_spec.__name__}"
                ),
            ) from ex
        except SupersetErrorsException:
            raise
        except Exception as ex:
            # Delegate to engine spec for structured SIP-40 errors
            # (CONNECTION_INVALID_HOSTNAME_ERROR, CONNECTION_ACCESS_DENIED_ERROR,
            # etc.).  Matches test_connection.py:184-193 — except the
            # original catches DBAPIError specifically because sync
            # SQLAlchemy wraps driver errors.  In async with asyncpg,
            # exceptions raised during pool checkout (e.g.
            # InvalidPasswordError) are NOT wrapped in DBAPIError, so
            # we catch Exception and let extract_errors pattern-match
            # the message.
            #
            # NOTE: liteset's extract_errors returns list[dict] while
            # the original returns list[SupersetError].  We pass the
            # dicts through as-is — they are already SIP-40 shaped.
            errors = database.db_engine_spec.extract_errors(ex, context)
            if errors:
                raise SupersetErrorsException(
                    errors=errors,
                    status_code=400,
                    message=errors[0].get("message", str(ex)),
                ) from ex
            # No custom_errors pattern matched — treat as unexpected
            logger.exception("Unexpected error during connection test")
            raise DatabaseTestConnectionUnexpectedError(
                errors=[
                    {
                        "message": (
                            "Unexpected error occurred, please check your "
                            "logs for details"
                        ),
                        "error_type": "GENERIC_DB_ENGINE_ERROR",
                        "level": "error",
                        "extra": {},
                    }
                ],
                status_code=422,
                message=str(ex),
            ) from ex


class ValidateSQLCommand(AsyncBaseCommand[dict[str, Any]]):
    """Validate SQL syntax using sqlglot.

    Ported from superset_old/commands/database/validate_sql.py.
    The original delegates to engine-specific validators; since engine
    specs are stubs in Liteset, we use sqlglot for dialect-aware
    syntax checking.  Errors are returned in the original format:
    ``[{"line_number": N, "start_column": N, "end_column": N, "message": "..."}]``
    """

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        sql: str,
        schema: str | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._sql = sql
        self._schema = schema
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._sql or not self._sql.strip():
            raise CommandInvalidError("SQL query is required")
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

    async def run(self) -> dict[str, Any]:
        import asyncio

        errors = await asyncio.to_thread(self._validate_with_sqlglot)
        return {"result": errors}

    def _validate_with_sqlglot(self) -> list[dict[str, Any]]:
        """Parse SQL with sqlglot and return any syntax errors.

        Runs in a thread pool because sqlglot is CPU-bound and fully
        synchronous.
        """
        import sqlglot
        from sqlglot.errors import ParseError

        # Map database backend to sqlglot dialect
        dialect = self._resolve_dialect()

        errors: list[dict[str, Any]] = []
        try:
            sqlglot.transpile(
                self._sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE
            )
        except ParseError as ex:
            # sqlglot ParseError includes error details in .errors
            for err in getattr(ex, "errors", []):
                line = err.get("line", 1)
                col = err.get("col", 0)
                description = err.get("description", str(ex))
                errors.append(
                    {
                        "line_number": line,
                        "start_column": col,
                        "end_column": col,
                        "message": description,
                    }
                )
            # If no structured errors were parsed, return the raw message
            if not errors:
                errors.append(
                    {
                        "line_number": 1,
                        "start_column": 0,
                        "end_column": 0,
                        "message": str(ex),
                    }
                )

        return errors

    def _resolve_dialect(self) -> str | None:
        """Map the database's SQLAlchemy URI backend to a sqlglot dialect."""
        if not self._database:
            return None
        uri = str(getattr(self._database, "sqlalchemy_uri", "") or "")
        if "://" not in uri:
            return None
        backend = uri.split("://")[0].split("+")[0].lower()
        backend_to_dialect: dict[str, str] = {
            "postgresql": "postgres",
            "mysql": "mysql",
            "sqlite": "sqlite",
            "mssql": "tsql",
            "clickhouse": "clickhouse",
            "trino": "trino",
            "presto": "presto",
            "hive": "hive",
            "bigquery": "bigquery",
            "snowflake": "snowflake",
            "redshift": "redshift",
            "duckdb": "duckdb",
            "oracle": "oracle",
            "spark": "spark",
            "databricks": "databricks",
        }
        return backend_to_dialect.get(backend)


BYPASS_VALIDATION_ENGINES = {"bigquery", "snowflake"}


class ValidateParametersCommand(AsyncBaseCommand[dict[str, Any]]):
    """Validate database engine parameters.

    Ported from superset_old/commands/database/validate.py.
    Delegates validation to the engine spec's ``validate_parameters``
    method, then optionally builds an ephemeral database and tries to
    connect.  Engines that are only validated on-create (BigQuery,
    Snowflake) are bypassed.
    """

    def __init__(
        self,
        data: dict[str, Any],
        dao: AsyncDatabaseDAO | None = None,
    ) -> None:
        self._data = data
        self._dao = dao
        self._model: Database | None = None

    async def validate(self) -> None:
        if not self._data.get("engine"):
            raise CommandInvalidError("engine is required")

        # If an existing database ID is provided, load it so we can
        # unmask encrypted extras later.
        database_id = self._data.get("id")
        if database_id is not None and self._dao is not None:
            self._model = await self._dao.find_by_id(database_id)

    async def run(self) -> dict[str, Any]:  # noqa: C901
        from superset.db_engine_specs import get_engine_spec

        engine = self._data["engine"]
        driver = self._data.get("driver")

        # Skip engines that are only validated on-create
        if engine in BYPASS_VALIDATION_ENGINES:
            return {"errors": []}

        spec_class = get_engine_spec(engine, driver)

        # Check that the engine supports parameter-based configuration
        if not hasattr(spec_class, "parameters_schema"):
            from superset.exceptions import SupersetErrorsException

            raise SupersetErrorsException(
                errors=[
                    {
                        "message": (
                            f'Engine "{engine}" cannot be configured '
                            f"through parameters."
                        ),
                        "error_type": "GENERIC_DB_ENGINE_ERROR",
                        "level": "error",
                        "extra": {},
                    }
                ],
                status_code=422,
                message=(
                    f'Engine "{engine}" cannot be configured through parameters.'
                ),
            )

        errors: list[dict[str, Any]] = []

        # Run engine-specific parameter validation.
        #
        # ``spec_class.validate_parameters`` is a synchronous classmethod
        # that calls ``is_hostname_valid`` / ``is_port_open`` — both of
        # which wrap ``socket.getaddrinfo`` and ``socket.connect``.  Those
        # block for seconds when DNS or the target host is down, which
        # starves the asyncio event loop and cascades into 5+ sequential
        # validate requests each taking 4s on a non-resolvable host like
        # ``badhost``.  In the original Flask backend each request was
        # on its own worker thread, so the blocking was hidden per-call.
        # Run the sync validator on the threadpool to restore that
        # concurrency model.
        import asyncio

        try:
            spec_errors = await asyncio.to_thread(
                spec_class.validate_parameters, self._data
            )
            if spec_errors:
                for err in spec_errors:
                    if isinstance(err, dict):
                        errors.append(err)
                    else:
                        # SupersetError objects — convert to SIP-40 dict
                        errors.append(
                            {
                                "message": getattr(err, "message", str(err)),
                                "error_type": getattr(
                                    err,
                                    "error_type",
                                    "GENERIC_DB_ENGINE_ERROR",
                                ),
                                "level": getattr(err, "level", "error"),
                                "extra": getattr(err, "extra", {}),
                            }
                        )
        except NotImplementedError:
            # Engine doesn't implement custom validation — fall through
            # to basic checks below.
            pass
        except Exception as ex:
            errors.append({"message": str(ex)})

        if errors:
            from superset.exceptions import SupersetErrorsException

            raise SupersetErrorsException(
                errors=errors,
                status_code=422,
                message=errors[0].get("message", "Validation error"),
            )

        # Basic required-field checks for parameter-based configs
        parameters = self._data.get("parameters", {})
        if parameters:
            for field_name in ("host", "database"):
                if not parameters.get(field_name):
                    errors.append(
                        {
                            "message": f"{field_name} is required",
                            "field": field_name,
                        }
                    )

        if errors:
            from superset.exceptions import SupersetErrorsException

            raise SupersetErrorsException(
                errors=errors,
                status_code=422,
                message=errors[0].get("message", "Validation error"),
            )

        return {"message": "OK"}


class ExportDatabasesCommand(AsyncExportModelsCommand):
    _resource_type = "Database"

    def __init__(
        self,
        model_ids: list[int],
        dao: AsyncDatabaseDAO | None = None,
    ) -> None:
        super().__init__(model_ids)
        self._dao = dao

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")
        database = await self._dao.find_by_id(model_id)
        if not database:
            raise ObjectNotFoundError("Database", model_id)

        files: list[tuple[str, str]] = []

        # -- Database YAML ---------------------------------------------------
        db_file_name = _safe_filename(database.database_name)
        payload: dict[str, Any] = {
            "database_name": database.database_name,
            "sqlalchemy_uri": mask_uri_password(str(database.sqlalchemy_uri)),
            "cache_timeout": database.cache_timeout,
            "expose_in_sqllab": getattr(database, "expose_in_sqllab", True),
            "allow_run_async": getattr(database, "allow_run_async", False),
            "allow_ctas": getattr(database, "allow_ctas", False),
            "allow_cvas": getattr(database, "allow_cvas", False),
            "allow_dml": getattr(database, "allow_dml", False),
            # V1 schema uses ``allow_csv_upload`` for backward compat
            "allow_csv_upload": getattr(database, "allow_file_upload", False),
            "extra": getattr(database, "extra", "{}"),
            "uuid": str(database.uuid) if getattr(database, "uuid", None) else None,
        }

        # Parse extra JSON and apply legacy renames
        if payload.get("extra"):
            extra = _parse_extra(str(payload["extra"]))
            # ``schemas_allowed_for_file_upload`` -> ``schemas_allowed_for_csv_upload``
            # for V1 schema backward compat
            if "schemas_allowed_for_file_upload" in extra:
                extra["schemas_allowed_for_csv_upload"] = extra.pop(
                    "schemas_allowed_for_file_upload"
                )
            payload["extra"] = extra

        # SSH tunnel export with masked passwords
        ssh_tunnel = await self._dao.get_ssh_tunnel(model_id)
        if ssh_tunnel:
            ssh_payload: dict[str, Any] = {
                "server_address": ssh_tunnel.server_address,
                "server_port": ssh_tunnel.server_port,
                "username": ssh_tunnel.username,
                "password": getattr(ssh_tunnel, "password", None),
                "private_key": getattr(ssh_tunnel, "private_key", None),
                "private_key_password": getattr(
                    ssh_tunnel, "private_key_password", None
                ),
            }
            payload["ssh_tunnel"] = _mask_ssh_tunnel_passwords(ssh_payload)

        payload["version"] = EXPORT_VERSION
        files.append(
            (
                f"databases/{db_file_name}.yaml",
                yaml.safe_dump(payload, sort_keys=False),
            )
        )

        # -- Related datasets -----------------------------------------------
        datasets = await self._dao.get_datasets(model_id)
        for dataset in datasets:
            ds_file_name = _safe_filename(getattr(dataset, "table_name", "unknown"))
            ds_payload = self._export_dataset_payload(dataset, database)
            files.append(
                (
                    f"datasets/{db_file_name}/{ds_file_name}.yaml",
                    yaml.safe_dump(ds_payload, sort_keys=False),
                )
            )

        return files

    @staticmethod
    def _export_dataset_payload(
        dataset: Any,
        database: Database,
    ) -> dict[str, Any]:
        """Build export dict for a dataset related to the exported database."""
        payload: dict[str, Any] = {
            "table_name": getattr(dataset, "table_name", ""),
            "main_dttm_col": getattr(dataset, "main_dttm_col", None),
            "description": getattr(dataset, "description", None),
            "default_endpoint": getattr(dataset, "default_endpoint", None),
            "offset": getattr(dataset, "offset", 0),
            "cache_timeout": getattr(dataset, "cache_timeout", None),
            "schema": getattr(dataset, "schema", None),
            "sql": getattr(dataset, "sql", None),
            "params": getattr(dataset, "params", None),
            "template_params": getattr(dataset, "template_params", None),
            "filter_select_enabled": getattr(dataset, "filter_select_enabled", True),
            "fetch_values_predicate": getattr(dataset, "fetch_values_predicate", None),
            "extra": getattr(dataset, "extra", None),
            "normalize_columns": getattr(dataset, "normalize_columns", False),
            "always_filter_main_dttm": getattr(
                dataset, "always_filter_main_dttm", False
            ),
            "uuid": (str(dataset.uuid) if getattr(dataset, "uuid", None) else None),
        }

        # Parse JSON string fields
        for key in ("params", "template_params", "extra"):
            if payload.get(key) and isinstance(payload[key], str):
                try:
                    payload[key] = json.loads(payload[key])
                except (json.JSONDecodeError, TypeError):
                    pass

        # Columns
        columns: list[dict[str, Any]] = []
        for col in getattr(dataset, "columns", []) or []:
            col_dict: dict[str, Any] = {
                "column_name": col.column_name,
                "verbose_name": getattr(col, "verbose_name", None),
                "is_dttm": getattr(col, "is_dttm", False),
                "is_active": getattr(col, "is_active", True),
                "type": getattr(col, "type", None),
                "advanced_data_type": getattr(col, "advanced_data_type", None),
                "groupby": getattr(col, "groupby", True),
                "filterable": getattr(col, "filterable", True),
                "expression": getattr(col, "expression", None),
                "description": getattr(col, "description", None),
                "python_date_format": getattr(col, "python_date_format", None),
                "extra": getattr(col, "extra", None),
                "uuid": (str(col.uuid) if getattr(col, "uuid", None) else None),
            }
            if col_dict.get("extra") and isinstance(col_dict["extra"], str):
                try:
                    col_dict["extra"] = json.loads(col_dict["extra"])
                except (json.JSONDecodeError, TypeError):
                    pass
            columns.append(col_dict)
        payload["columns"] = columns

        # Metrics
        metrics: list[dict[str, Any]] = []
        for m in getattr(dataset, "metrics", []) or []:
            m_dict: dict[str, Any] = {
                "metric_name": m.metric_name,
                "verbose_name": getattr(m, "verbose_name", None),
                "metric_type": getattr(m, "metric_type", None),
                "expression": m.expression,
                "description": getattr(m, "description", None),
                "d3format": getattr(m, "d3format", None),
                "currency": getattr(m, "currency", None),
                "extra": getattr(m, "extra", None),
                "warning_text": getattr(m, "warning_text", None),
                "uuid": (str(m.uuid) if getattr(m, "uuid", None) else None),
            }
            if m_dict.get("extra") and isinstance(m_dict["extra"], str):
                try:
                    m_dict["extra"] = json.loads(m_dict["extra"])
                except (json.JSONDecodeError, TypeError):
                    pass
            metrics.append(m_dict)
        payload["metrics"] = metrics

        payload["version"] = EXPORT_VERSION
        payload["database_uuid"] = (
            str(database.uuid) if getattr(database, "uuid", None) else None
        )
        return payload


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


class UploadCommand(AsyncBaseCommand[dict[str, Any]]):
    """Upload a file to a database as a new table.

    Ported from superset_old/commands/database/uploaders/base.py.
    Reads file contents into a DataFrame and uploads to the database
    using the engine spec's df_to_sql method. Creates a SqlaTable
    entry if it doesn't exist.
    """

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        data: dict[str, Any],
        file_contents: bytes,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._data = data
        self._file_contents = file_contents
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._data.get("table_name"):
            raise CommandInvalidError("table_name is required")
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

        # Check if file upload is allowed for this database/schema
        if not getattr(self._database, "allow_file_upload", False):
            raise CommandInvalidError("File upload is not enabled for this database")

    async def run(self) -> dict[str, Any]:

        from superset.sql.parse import Table

        table_name = self._data["table_name"]
        schema_name = self._data.get("schema")
        file_type = self._data.get("file_type", "csv")

        # Read file into DataFrame
        df = self._read_file(file_type)

        # Upload DataFrame to database
        data_table = Table(table=table_name, schema=schema_name)
        to_sql_kwargs = {
            "chunksize": 1000,
            "if_exists": self._data.get("if_exists", "fail"),
            "index": self._data.get("dataframe_index", False),
        }
        if self._data.get("index_label") and self._data.get("dataframe_index"):
            to_sql_kwargs["index_label"] = self._data["index_label"]

        self._database.db_engine_spec.df_to_sql(
            self._database,
            data_table,
            df,
            to_sql_kwargs=to_sql_kwargs,
        )

        # Create or update SqlaTable entry
        from sqlalchemy import select

        from superset.models.connectors import SqlaTable

        stmt = select(SqlaTable).where(
            SqlaTable.table_name == table_name,
            SqlaTable.schema == schema_name,
            SqlaTable.database_id == self._database_id,
        )
        result = await self._dao.session.execute(stmt)
        sqla_table = result.scalars().one_or_none()

        if not sqla_table:
            sqla_table = SqlaTable(
                table_name=table_name,
                database_id=self._database_id,
                schema=schema_name,
            )
            self._dao.session.add(sqla_table)

        await self._dao.session.flush()

        return {"message": "OK", "table_id": sqla_table.id}

    def _read_file(self, file_type: str) -> pd.DataFrame:
        """Read file contents into a pandas DataFrame."""
        file_obj = io.BytesIO(self._file_contents)

        if file_type == "csv":
            return pd.read_csv(file_obj)
        elif file_type == "excel":
            return pd.read_excel(file_obj)
        elif file_type == "columnar":
            return pd.read_parquet(file_obj)
        else:
            raise CommandInvalidError(f"Unsupported file type: {file_type}")


class SyncPermissionsCommand(AsyncBaseCommand[dict[str, Any]]):
    """Sync database permissions.

    Ported from superset_old/commands/database/sync_permissions.py.
    Syncs catalog and schema permissions from the database to the
    security manager, creating new permission entries as needed.
    """

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        security_manager: Any | None = None,
        username: str | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._security_manager = security_manager
        self._username = username
        self._database: Any | None = None

    async def validate(self) -> None:
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

    async def run(self) -> dict[str, Any]:

        if self._security_manager is None:
            return {"message": "Security manager not provided"}

        catalog_perm_count = 0
        schema_perm_count = 0

        # Get catalog names from the database
        catalogs = await self._get_catalog_names()

        for catalog in catalogs:
            try:
                schemas = await self._get_schema_names(catalog)

                # Process catalog permissions
                if catalog:
                    perm = self._security_manager.get_catalog_perm(
                        self._database.database_name,
                        catalog,
                    )
                    existing_pvm = self._security_manager.find_permission_view_menu(
                        "catalog_access",
                        perm,
                    )
                    if not existing_pvm:
                        # New catalog - add permission
                        self._security_manager.add_permission_view_menu(
                            "catalog_access",
                            perm,
                        )
                        catalog_perm_count += 1

                        # Add schema permissions for this catalog
                        for schema in schemas:
                            schema_perm = self._security_manager.get_schema_perm(
                                self._database.database_name,
                                catalog,
                                schema,
                            )
                            existing_schema_pvm = (
                                self._security_manager.find_permission_view_menu(
                                    "schema_access",
                                    schema_perm,
                                )
                            )
                            if not existing_schema_pvm:
                                self._security_manager.add_permission_view_menu(
                                    "schema_access",
                                    schema_perm,
                                )
                                schema_perm_count += 1
                        continue

                # Add new schemas that don't have permissions yet
                for schema in schemas:
                    schema_perm = self._security_manager.get_schema_perm(
                        self._database.database_name,
                        catalog,
                        schema,
                    )
                    existing_schema_pvm = (
                        self._security_manager.find_permission_view_menu(
                            "schema_access",
                            schema_perm,
                        )
                    )
                    if not existing_schema_pvm:
                        self._security_manager.add_permission_view_menu(
                            "schema_access",
                            schema_perm,
                        )
                        schema_perm_count += 1

            except Exception:
                logger.warning(
                    "Error processing catalog %s",
                    catalog or "(default)",
                    exc_info=True,
                )
                continue

        await self._dao.session.flush()

        return {
            "message": "OK",
            "catalog_permissions_added": catalog_perm_count,
            "schema_permissions_added": schema_perm_count,
        }

    async def _get_catalog_names(self) -> set[str | None]:
        """Get all catalog names from the database."""
        if not getattr(self._database.db_engine_spec, "supports_catalog", False):
            return {None}

        try:
            # If the database doesn't support cross-catalog queries or
            # multi-catalog is not enabled, only use the default catalog
            if getattr(
                self._database.db_engine_spec, "supports_cross_catalog_queries", False
            ) or getattr(self._database, "allow_multi_catalog", False):
                return self._database.get_all_catalog_names(force=True)
            else:
                return {self._database.get_default_catalog()}
        except Exception:
            logger.warning(
                "Failed to get catalog names",
                exc_info=True,
            )
            return {None}

    async def _get_schema_names(self, catalog: str | None) -> set[str]:
        """Get all schema names for a catalog."""
        try:
            return self._database.get_all_schema_names(
                force=True,
                catalog=catalog,
            )
        except Exception:
            logger.warning(
                "Failed to get schema names for catalog %s",
                catalog or "(default)",
                exc_info=True,
            )
            return set()


class DeleteSSHTunnelCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._tunnel: Any = None

    async def validate(self) -> None:
        self._tunnel = await self._dao.get_ssh_tunnel(self._database_id)
        if not self._tunnel:
            raise ObjectNotFoundError("SSHTunnel", self._database_id)

    async def run(self) -> None:
        assert self._tunnel is not None
        await self._dao.session.delete(self._tunnel)
        await self._dao.session.flush()
