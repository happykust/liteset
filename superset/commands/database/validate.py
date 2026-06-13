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
"""Async port of ``superset_old/commands/database/validate.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.models.core import Database

logger = logging.getLogger(__name__)

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
                message=(f'Engine "{engine}" cannot be configured through parameters.'),
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
        # ``badhost``.  In the original backend each request was
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

        # Connection ping — 1:1 with upstream
        # ``ValidateDatabaseParametersCommand.run``: the engine-spec checks
        # above only verify hostname/port reachability, so build an ephemeral
        # DB from the parameters and actually connect (``do_ping``). This
        # catches a reachable host with WRONG credentials / a non-DB service
        # (the port previously returned OK → user got a false "valid").
        import json as _json

        from superset.commands.database.test_connection import _ping
        from superset.databases.utils import make_url_safe
        from superset.exceptions import SupersetErrorsException

        serialized_encrypted_extra = self._data.get("masked_encrypted_extra", "{}")
        if self._model is not None:
            serialized_encrypted_extra = spec_class.unmask_encrypted_extra(
                self._model.encrypted_extra, serialized_encrypted_extra
            )
        try:
            encrypted_extra = _json.loads(serialized_encrypted_extra or "{}")
        except (ValueError, TypeError):
            encrypted_extra = {}

        sqlalchemy_uri = spec_class.build_sqlalchemy_uri(parameters, encrypted_extra)
        if (
            self._model is not None
            and sqlalchemy_uri == self._model.safe_sqlalchemy_uri()
        ):
            sqlalchemy_uri = self._model.sqlalchemy_uri_decrypted

        database = self._dao.build_db_for_connection_test(
            server_cert=self._data.get("server_cert", "") or "",
            extra=self._data.get("extra", "{}") or "{}",
            impersonate_user=self._data.get("impersonate_user", False),
            encrypted_extra=_json.dumps(encrypted_extra),
        )
        # If the URI still carries the password MASK (an existing-DB validation
        # where the real password wasn't unmasked — e.g. only non-credential
        # params were echoed back), pinging would always auth-fail. Skip the
        # ping in that case so editing an existing DB doesn't false-fail; the
        # engine-spec param validation above already ran.
        from superset.constants import PASSWORD_MASK

        if PASSWORD_MASK in (sqlalchemy_uri or ""):
            return {"message": "OK"}

        database.set_sqlalchemy_uri(sqlalchemy_uri)
        database.db_engine_spec.mutate_db_for_connection_test(database)

        def _do_ping() -> tuple[bool, Exception | None]:
            try:
                with database.get_sqla_engine() as engine:
                    return bool(_ping(engine)), None
            except Exception as ex:  # noqa: BLE001
                return False, ex

        alive, ping_error = await asyncio.to_thread(_do_ping)
        if not alive:
            # OAuth2-needed → accept (flow triggers on first query); 1:1 upstream.
            if (
                ping_error is not None
                and database.is_oauth2_enabled()
                and database.db_engine_spec.needs_oauth2(ping_error)
            ):
                return {"message": "OK"}
            if ping_error is not None:
                url = make_url_safe(sqlalchemy_uri)
                context = {
                    "hostname": url.host,
                    "password": url.password,
                    "port": url.port,
                    "username": url.username,
                    "database": url.database,
                }
                conn_errors = database.db_engine_spec.extract_errors(
                    ping_error, context
                )
                raise SupersetErrorsException(conn_errors, status=400) from ping_error
            # Reachable + connected but ping returned False → offline (422).
            raise SupersetErrorsException(
                errors=[
                    {
                        "message": "Database is offline.",
                        "error_type": "GENERIC_DB_ENGINE_ERROR",
                        "level": "error",
                        "extra": {},
                    }
                ],
                status_code=422,
                message="Database is offline.",
            )

        return {"message": "OK"}
