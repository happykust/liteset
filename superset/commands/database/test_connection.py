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
"""Async port of ``superset_old/commands/database/test_connection.py``."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import closing
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.databases.utils import DatabaseInvalidError
from superset.exceptions import CommandInvalidError
from superset.i18n import gettext as _

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.models.core import Database

logger = logging.getLogger(__name__)


class DatabaseSecurityUnsafeError(CommandInvalidError):
    """Raised when the connection settings are deemed unsafe.

    1:1 with ``superset_old/commands/database/exceptions.py`` (which is not
    yet ported in the async ``commands/database/exceptions`` module, so the
    class is defined locally here).
    """

    status_code = 422
    message = _("Stopped an unsafe database connection")


def _ping(engine: Any) -> bool:
    """Ping ``engine`` to verify connectivity.

    1:1 with ``superset_old/commands/database/utils.py::ping`` — opens a raw
    DBAPI connection and runs the dialect's ``do_ping``.  Always executed
    inside a worker thread (via :func:`asyncio.to_thread`) because the
    underlying SQLAlchemy engine is synchronous.  ``SigalrmTimeout`` is a
    no-op off the main thread, matching the original's defensive fallback.
    """
    import sqlite3

    from superset.utils.core import SigalrmTimeout

    try:
        seconds = _ping_timeout_seconds()
        with SigalrmTimeout(seconds=seconds):
            with closing(engine.raw_connection()) as conn:
                return engine.dialect.do_ping(conn)
    except (sqlite3.ProgrammingError, RuntimeError):
        # SQLite can't run on a separate thread, so ``utils.timeout`` fails.
        # RuntimeError catches the equivalent error from duckdb.
        return engine.dialect.do_ping(engine)


def _ping_timeout_seconds() -> int:
    """Return ``TEST_DATABASE_CONNECTION_TIMEOUT`` in whole seconds."""
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        value = getattr(settings, "test_database_connection_timeout", 30)
    except Exception:  # noqa: BLE001
        value = 30
    # Original config holds a ``timedelta``; the ported settings expose an
    # int.  Support both for safety.
    total = getattr(value, "total_seconds", None)
    return int(total()) if callable(total) else int(value)


class DatabaseTestConnectionCommand(AsyncBaseCommand[dict[str, Any]]):
    """Test database connectivity.

    Ported 1:1 from ``superset_old/commands/database/test_connection.py``.
    Builds an ephemeral Database model from the payload, resolves the URI
    (including existing-model URI decryption for masked URIs), unmasks
    ``encrypted_extra`` against the persisted model, builds the optional
    :class:`SSHTunnel` from the payload, and pings the engine (through the
    SSH tunnel when one is supplied) to verify reachability.
    """

    __test__ = False  # prevent pytest collection

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        default_redirect_uri: str | None = None,
    ) -> None:
        self._dao = dao
        self._properties = dict(data)
        self._user_id = user_id
        # Used to start the OAuth2 dance — usually the absolute
        # ``/api/v1/database/oauth2/`` URI of the running Liteset instance.
        self._default_redirect_uri = default_redirect_uri or "/api/v1/database/oauth2/"
        self._model: Database | None = None
        self._context: dict[str, Any] = {}
        self._uri: str = ""

    async def _resolve_model_and_uri(self) -> None:
        """Resolve the existing model (by name) and the effective URI.

        Mirrors ``TestConnectionDatabaseCommand.__init__`` lines 69-90 in the
        original — that work happens synchronously in ``__init__`` upstream,
        but we defer it here because loading the model needs the async DAO.
        """
        from superset.databases.utils import make_url_safe

        if (database_name := self._properties.get("database_name")) is not None:
            self._model = await self._dao.get_database_by_name(database_name)

        uri = self._properties.get("sqlalchemy_uri", "")
        if self._model and uri == self._model.safe_sqlalchemy_uri():
            uri = self._model.sqlalchemy_uri_decrypted

        url = make_url_safe(uri)
        self._context = {
            "hostname": url.host,
            "password": url.password,
            "port": url.port,
            "username": url.username,
            "database": url.database,
        }
        self._uri = uri

    def _build_uri_from_parameters(self) -> None:
        """Build ``sqlalchemy_uri`` from individual parameters (dynamic_form).

        Upstream this happens in the Marshmallow ``@pre_load``
        ``DatabaseParametersSchemaMixin.build_sqlalchemy_uri`` hook
        (superset_old/databases/schemas.py:304-363) which
        ``DatabaseTestConnectionSchema`` inherits — so the command always
        received a ready URI.  msgspec has no pre-load stage; do the same
        transformation here (mirrors ``CreateDatabaseCommand.validate``).
        """
        parameters = self._properties.pop("parameters", {}) or {}
        engine = (
            self._properties.pop("engine", None)
            or (
                parameters.pop("engine", None) if isinstance(parameters, dict) else None
            )
            or self._properties.pop("backend", None)
        )
        driver = self._properties.pop("driver", None)

        if (
            self._properties.get("sqlalchemy_uri")
            or self._properties.get("configuration_method") != "dynamic_form"
        ):
            return

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

        encrypted_extra_str = self._properties.get("masked_encrypted_extra") or "{}"
        try:
            encrypted_extra = json.loads(encrypted_extra_str)
        except (ValueError, TypeError):
            encrypted_extra = {}

        try:
            self._properties["sqlalchemy_uri"] = spec_class.build_sqlalchemy_uri(
                parameters,
                encrypted_extra,
            )
        except ValueError as ex:
            # Engine specs (e.g. BigQuery) raise ValueError for missing /
            # invalid credentials — upstream's @pre_load surfaced these as
            # a Marshmallow ValidationError → 400/422, not a 500.
            raise CommandInvalidError(str(ex)) from ex

    async def validate(self) -> None:
        from superset.commands.database.ssh_tunnel.exceptions import (
            SSHTunnelDatabasePortError,
            SSHTunnelingNotEnabledError,
        )
        from superset.utils.feature_flags import feature_flag_manager

        self._build_uri_from_parameters()

        uri = self._properties.get("sqlalchemy_uri")
        if not uri:
            raise CommandInvalidError("sqlalchemy_uri is required for connection test")

        try:
            await self._resolve_model_and_uri()
        except DatabaseInvalidError as ex:
            # ``make_url_safe`` raises this for any unparseable URI
            # (e.g. ``not-a-valid-uri``). Upstream surfaces every
            # ``set_sqlalchemy_uri`` / URL-parse failure inside the same
            # outer try in run() and wraps it as
            # ``DatabaseTestConnectionUnexpectedError`` → 422; do the same
            # here so client toasts show a real message instead of 500.
            raise CommandInvalidError(
                "Invalid SQLAlchemy URI — host/port/driver could not be "
                "parsed; double-check your connection string."
            ) from ex

        # Matches ``TestConnectionDatabaseCommand.validate`` (lines 227-233):
        # an SSH tunnel requires the feature flag plus a non-empty port.
        if self._properties.get("ssh_tunnel"):
            if not feature_flag_manager.is_feature_enabled("SSH_TUNNELING"):
                raise SSHTunnelingNotEnabledError()
            if not self._context.get("port"):
                raise SSHTunnelDatabasePortError()

    async def run(self) -> dict[str, Any]:  # noqa: C901
        from sqlalchemy.exc import DBAPIError, NoSuchModuleError

        from superset.commands.database.ssh_tunnel.exceptions import (
            SSHTunnelingNotEnabledError,
        )
        from superset.exceptions import (
            DatabaseTestConnectionDriverError,
            DatabaseTestConnectionUnexpectedError,
            OAuth2RedirectError,
            SupersetErrorsException,
            SupersetSecurityException,
            SupersetTimeoutException,
        )
        from superset.models.ssh_tunnel import SSHTunnel
        from superset.utils.ssh_tunnel import unmask_password_info

        if not self._uri:
            await self._resolve_model_and_uri()

        ex_str = ""
        ssh_tunnel = self._properties.get("ssh_tunnel")

        # Unmask ``encrypted_extra`` against the persisted model so masked
        # placeholders are replaced with the real stored secret before the
        # test — matches test_connection.py:99-109.
        serialized_encrypted_extra = self._properties.get(
            "masked_encrypted_extra",
            "{}",
        )
        if self._model:
            serialized_encrypted_extra = (
                self._model.db_engine_spec.unmask_encrypted_extra(
                    self._model.encrypted_extra,
                    serialized_encrypted_extra,
                )
            )

        database = self._dao.build_db_for_connection_test(
            server_cert=self._properties.get("server_cert", ""),
            extra=self._properties.get("extra", "{}"),
            impersonate_user=self._properties.get("impersonate_user", False),
            encrypted_extra=serialized_encrypted_extra,
        )

        database.set_sqlalchemy_uri(self._uri)
        database.db_engine_spec.mutate_db_for_connection_test(database)

        # Build the SSHTunnel from the payload (when present), unmasking any
        # masked credential fields against the existing tunnel row first —
        # matches test_connection.py:122-130.
        if ssh_tunnel:
            ssh_tunnel = dict(ssh_tunnel)
            if ssh_tunnel_id := ssh_tunnel.pop("id", None):
                existing_ssh_tunnel = await self._find_ssh_tunnel_by_id(ssh_tunnel_id)
                if existing_ssh_tunnel:
                    ssh_tunnel = unmask_password_info(ssh_tunnel, existing_ssh_tunnel)
            ssh_tunnel = SSHTunnel(**ssh_tunnel)

        try:
            # Ping through the (sync) engine, optionally via the SSH tunnel.
            # The sync engine is the only path that supports
            # ``override_ssh_tunnel`` (it opens/tears down the tunnel through
            # the SSHManager), mirroring the original ``ping(engine)`` call.
            #
            # ``_ping_database`` mirrors the original inner ``try/except`` block
            # (test_connection.py:138-161): a ``SupersetTimeoutException`` is
            # re-raised as a connection-timeout SIP-40 error, while any other
            # failure is swallowed into ``(alive=False, ping_error=ex)`` so the
            # post-block logic below can run the OAuth2 dance (which is async
            # and therefore cannot run inside the worker thread) and then raise
            # a ``DBAPIError`` — matching the original control flow exactly.
            alive, ping_error = await asyncio.to_thread(
                self._ping_database, database, ssh_tunnel
            )
            if not alive:
                if (
                    ping_error is not None
                    and self._user_id is not None
                    and database.is_oauth2_enabled()
                    and database.db_engine_spec.needs_oauth2(ping_error)
                ):
                    await database.db_engine_spec.start_oauth2_dance(
                        database,
                        user_id=self._user_id,
                        default_redirect_uri=self._default_redirect_uri,
                    )
                # So we stop losing the original message if any.
                ex_str = str(ping_error) if ping_error is not None else ""
                raise DBAPIError(ex_str or None, None, None)
            return {"message": "OK"}

        except (NoSuchModuleError, ModuleNotFoundError) as ex:
            raise DatabaseTestConnectionDriverError(
                message=(
                    f"Could not load database driver: "
                    f"{database.db_engine_spec.__name__}"
                ),
            ) from ex
        except DBAPIError as ex:
            # Custom errors (wrong username, wrong password, etc).
            errors = database.db_engine_spec.extract_errors(ex, self._context)
            raise SupersetErrorsException(errors, status=400) from ex
        except OAuth2RedirectError:
            raise
        except SupersetSecurityException as ex:
            raise DatabaseSecurityUnsafeError(message=str(ex)) from ex
        except (SupersetTimeoutException, SSHTunnelingNotEnabledError):
            # bubble up the exception to return proper status code
            raise
        except SupersetErrorsException:
            raise
        except Exception as ex:
            # If the connection failed because OAuth2 is needed, start the
            # dance.  Mirrors test_connection.py:213-217.
            if (
                self._user_id is not None
                and database.is_oauth2_enabled()
                and database.db_engine_spec.needs_oauth2(ex)
            ):
                await database.db_engine_spec.start_oauth2_dance(
                    database,
                    user_id=self._user_id,
                    default_redirect_uri=self._default_redirect_uri,
                )
            errors = database.db_engine_spec.extract_errors(ex, self._context)
            raise DatabaseTestConnectionUnexpectedError(errors) from ex

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _ping_database(
        self, database: Database, ssh_tunnel: Any | None
    ) -> tuple[bool, Exception | None]:
        """Open a sync engine (through the tunnel when set) and ping it.

        Mirrors the original ``with database.get_sqla_engine(
        override_ssh_tunnel=ssh_tunnel) as engine:`` block
        (test_connection.py:137-161):

        * ``SupersetTimeoutException`` is re-raised as a connection-timeout
          SIP-40 error (lines 140-150).
        * any other exception is captured and returned as
          ``(False, ex)`` so the async caller can run the OAuth2 dance and
          raise the ``DBAPIError`` (the original does this *after* the
          ``with`` block, lines 151-164).

        Returns ``(alive, ping_error)``.
        """
        from superset.errors import ErrorLevel, SupersetErrorType
        from superset.exceptions import SupersetTimeoutException

        with database.get_sqla_engine(override_ssh_tunnel=ssh_tunnel) as engine:
            try:
                return _ping(engine), None
            except SupersetTimeoutException as ex:
                raise SupersetTimeoutException(
                    error_type=SupersetErrorType.CONNECTION_DATABASE_TIMEOUT,
                    message=(
                        "Please check your connection details and database "
                        "settings, and ensure that your database is accepting "
                        "connections, then try connecting again."
                    ),
                    level=ErrorLevel.ERROR,
                    extra={"sqlalchemy_uri": database.sqlalchemy_uri},
                ) from ex
            except Exception as ex:  # noqa: BLE001
                return False, ex

    async def _find_ssh_tunnel_by_id(self, ssh_tunnel_id: int) -> Any | None:
        """Look up an existing ``SSHTunnel`` row by primary key.

        Mirrors ``SSHTunnelDAO.find_by_id(ssh_tunnel_id)`` from the original;
        the async ``AsyncSSHTunnelDAO`` exposes lookups by database id only,
        so we query the session directly here.
        """
        from superset.models.ssh_tunnel import SSHTunnel

        session = getattr(self._dao, "session", None)
        if session is None:
            return None
        return await session.get(SSHTunnel, ssh_tunnel_id)
