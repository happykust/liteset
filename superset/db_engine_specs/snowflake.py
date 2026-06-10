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
"""Snowflake engine spec -- sync/Flask-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/snowflake.py`` with Flask
imports removed.  Only overridden methods and attributes are included.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from re import Pattern
from typing import Any, TYPE_CHECKING, TypedDict
from urllib import parse

from sqlalchemy import types
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import URL

from superset.constants import TimeGrain
from superset.databases.utils import make_url_safe
from superset.db_engine_specs.base import BaseEngineSpec, BasicPropertiesType
from superset.db_engine_specs.postgres import PostgresBaseEngineSpec
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.utils import json as json_utils

if TYPE_CHECKING:
    from superset.models.core import Database
    from superset.models.sql_lab import Query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regular expressions to catch custom errors
# ---------------------------------------------------------------------------

OBJECT_DOES_NOT_EXIST_REGEX = re.compile(
    r"Object (?P<object>.*?) does not exist or not authorized."
)

SYNTAX_ERROR_REGEX = re.compile(
    "syntax error line (?P<line>.+?) at position (?P<position>.+?) "
    "unexpected '(?P<syntax_error>.+?)'."
)


class SnowflakeParametersType(TypedDict):
    username: str
    password: str
    account: str
    database: str
    role: str
    warehouse: str


# The original code uses a Marshmallow ``SnowflakeParametersSchema``.
# In liteset we store the OpenAPI fragment directly so that
# ``parameters_json_schema()`` returns it as-is (same pattern as
# ``BASIC_PARAMETERS_JSON_SCHEMA`` in base.py).
SNOWFLAKE_PARAMETERS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": {"type": "string", "description": "Username"},
        "password": {"type": "string", "description": "Password"},
        "account": {"type": "string", "description": "Account"},
        "database": {"type": "string", "description": "Database name"},
        "role": {"type": "string", "description": "Role"},
        "warehouse": {"type": "string", "description": "Warehouse"},
    },
    "required": ["username", "password", "account", "database", "role", "warehouse"],
}


class SnowflakeEngineSpec(PostgresBaseEngineSpec):
    engine = "snowflake"
    engine_name = "Snowflake"
    force_column_alias_quotes = True
    max_column_name_length = 256

    # Snowflake doesn't support IS true/false syntax, use = true/false instead
    use_equality_for_boolean_filters = True

    parameters_schema = SNOWFLAKE_PARAMETERS_JSON_SCHEMA
    default_driver = "snowflake"
    sqlalchemy_uri_placeholder = "snowflake://"

    supports_dynamic_schema = True
    supports_catalog = True
    supports_dynamic_catalog = True
    supports_cross_catalog_queries = True

    encrypted_extra_sensitive_fields = {
        "$.auth_params.privatekey_body",
        "$.auth_params.privatekey_pass",
    }

    _time_grain_expressions = {
        None: "{col}",
        TimeGrain.SECOND: "DATE_TRUNC('SECOND', {col})",
        TimeGrain.MINUTE: "DATE_TRUNC('MINUTE', {col})",
        TimeGrain.FIVE_MINUTES: (
            "DATEADD(MINUTE, "
            "FLOOR(DATE_PART(MINUTE, {col}) / 5) * 5, DATE_TRUNC('HOUR', {col}))"
        ),
        TimeGrain.TEN_MINUTES: (
            "DATEADD(MINUTE, "
            "FLOOR(DATE_PART(MINUTE, {col}) / 10) * 10, DATE_TRUNC('HOUR', {col}))"
        ),
        TimeGrain.FIFTEEN_MINUTES: (
            "DATEADD(MINUTE, "
            "FLOOR(DATE_PART(MINUTE, {col}) / 15) * 15, DATE_TRUNC('HOUR', {col}))"
        ),
        TimeGrain.THIRTY_MINUTES: (
            "DATEADD(MINUTE, "
            "FLOOR(DATE_PART(MINUTE, {col}) / 30) * 30, DATE_TRUNC('HOUR', {col}))"
        ),
        TimeGrain.HOUR: "DATE_TRUNC('HOUR', {col})",
        TimeGrain.DAY: "DATE_TRUNC('DAY', {col})",
        TimeGrain.WEEK: "DATE_TRUNC('WEEK', {col})",
        TimeGrain.MONTH: "DATE_TRUNC('MONTH', {col})",
        TimeGrain.QUARTER: "DATE_TRUNC('QUARTER', {col})",
        TimeGrain.YEAR: "DATE_TRUNC('YEAR', {col})",
    }

    custom_errors: dict[Pattern[str], tuple[str, str, dict[str, Any]]] = {
        OBJECT_DOES_NOT_EXIST_REGEX: (
            "%(object)s does not exist in this database.",
            "OBJECT_DOES_NOT_EXIST_ERROR",
            {},
        ),
        SYNTAX_ERROR_REGEX: (
            "Please check your query for syntax errors at or "
            'near "%(syntax_error)s". Then, try running your query again.',
            "SYNTAX_ERROR",
            {},
        ),
    }

    @staticmethod
    def get_extra_params(database: Database, source: Any = None) -> dict[str, Any]:
        """
        Add a user agent to be used in the requests.
        """
        from superset.utils.core import get_user_agent

        extra: dict[str, Any] = BaseEngineSpec.get_extra_params(database)
        engine_params: dict[str, Any] = extra.setdefault("engine_params", {})
        connect_args: dict[str, Any] = engine_params.setdefault("connect_args", {})
        user_agent = get_user_agent(database, source)

        connect_args.setdefault("application", user_agent)

        return extra

    @classmethod
    def adjust_engine_params(
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        if "/" in uri.database:
            current_catalog, current_schema = uri.database.split("/", 1)
        else:
            current_catalog, current_schema = uri.database, None

        adjusted_database = "/".join(
            [
                catalog or current_catalog,
                schema or current_schema or "",
            ]
        ).rstrip("/")

        uri = uri.set(database=adjusted_database)
        return uri, connect_args

    @classmethod
    def get_schema_from_engine_params(
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> str | None:
        database = sqlalchemy_uri.database.strip("/")

        if "/" not in database:
            return None

        return parse.unquote(database.split("/")[1])

    @classmethod
    def get_default_catalog(cls, database: Database) -> str:
        return database.url_object.database.split("/")[0]

    @classmethod
    def get_catalog_names(
        cls,
        database: Database,
        inspector: Inspector,
    ) -> set[str]:
        return {
            catalog
            for (catalog,) in inspector.bind.execute(
                "SELECT DATABASE_NAME from information_schema.databases"
            )
        }

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "DATEADD(S, {col}, '1970-01-01')"

    @classmethod
    def epoch_ms_to_dttm(cls) -> str:
        return "DATEADD(MS, {col}, '1970-01-01')"

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"TO_DATE('{dttm.date().isoformat()}')"
        if isinstance(sqla_type, types.TIMESTAMP):
            return f"""TO_TIMESTAMP('{dttm.isoformat(timespec="microseconds")}')"""
        if isinstance(sqla_type, types.DateTime):
            return f"""CAST('{dttm.isoformat(timespec="microseconds")}' AS DATETIME)"""
        return None

    @staticmethod
    def mutate_db_for_connection_test(database: Database) -> None:
        """By default, snowflake doesn't validate if the user/role has access
        to the chosen database.
        """
        extra = json_utils.loads(database.extra or "{}")
        engine_params = extra.get("engine_params", {})
        connect_args = engine_params.get("connect_args", {})
        connect_args["validate_default_parameters"] = True
        engine_params["connect_args"] = connect_args
        extra["engine_params"] = engine_params
        database.extra = json_utils.dumps(extra)

    @classmethod
    def get_cancel_query_id(cls, cursor: Any, query: Query) -> str | None:
        cursor.execute("SELECT CURRENT_SESSION()")
        row = cursor.fetchone()
        return row[0]

    @classmethod
    def cancel_query(cls, cursor: Any, query: Query, cancel_query_id: str) -> bool:
        try:
            cursor.execute(f"SELECT SYSTEM$CANCEL_ALL_QUERIES({cancel_query_id})")
        except Exception:  # noqa: BLE001
            return False
        return True

    @classmethod
    def build_sqlalchemy_uri(
        cls,
        parameters: SnowflakeParametersType,
        encrypted_extra: dict[str, Any] | None = None,
    ) -> str:
        return URL.create(
            "snowflake",
            username=parameters.get("username"),
            password=parameters.get("password"),
            host=parameters.get("account"),
            database=parameters.get("database"),
            query={
                "role": parameters.get("role"),
                "warehouse": parameters.get("warehouse"),
            },
        ).render_as_string(hide_password=False)

    @classmethod
    def get_parameters_from_uri(
        cls,
        uri: str,
        encrypted_extra: dict[str, str] | None = None,
    ) -> Any:
        url = make_url_safe(uri)
        query = dict(url.query.items())
        return {
            "username": url.username,
            "password": url.password,
            "account": url.host,
            "database": url.database,
            "role": query.get("role"),
            "warehouse": query.get("warehouse"),
        }

    @classmethod
    def validate_parameters(
        cls, properties: BasicPropertiesType
    ) -> list[SupersetError]:
        errors: list[SupersetError] = []
        required = {
            "warehouse",
            "username",
            "database",
            "account",
            "role",
            "password",
        }
        parameters = properties.get("parameters", {})
        present = {key for key in parameters if parameters.get(key, ())}

        if missing := sorted(required - present):
            errors.append(
                SupersetError(
                    message=f"One or more parameters are missing: {', '.join(missing)}",
                    error_type=SupersetErrorType.CONNECTION_MISSING_PARAMETERS_ERROR,
                    level=ErrorLevel.WARNING,
                    extra={"missing": missing},
                ),
            )
        return errors

    @classmethod
    def parameters_json_schema(cls) -> Any:
        """
        Return configuration parameters as OpenAPI.
        """
        if not cls.parameters_schema:
            return None

        return cls.parameters_schema

    @staticmethod
    def update_params_from_encrypted_extra(
        database: Database,
        params: dict[str, Any],
    ) -> None:
        if not database.encrypted_extra:
            return
        try:
            encrypted_extra = json_utils.loads(database.encrypted_extra)
        except json_utils.JSONDecodeError as ex:
            logger.error(ex, exc_info=True)
            raise
        auth_method = encrypted_extra.get("auth_method", None)
        auth_params = encrypted_extra.get("auth_params", {})
        if not auth_method:
            return
        connect_args = params.setdefault("connect_args", {})
        if auth_method == "keypair":
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import serialization

            privatekey_body = auth_params.get("privatekey_body", None)
            key = None
            if privatekey_body:
                key = privatekey_body.encode()
            else:
                with open(auth_params["privatekey_path"], "rb") as key_temp:
                    key = key_temp.read()
            privatekey_pass = auth_params.get("privatekey_pass", None)
            password = privatekey_pass.encode() if privatekey_pass is not None else None
            p_key = serialization.load_pem_private_key(
                key,
                password=password,
                backend=default_backend(),
            )
            pkb = p_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            connect_args["private_key"] = pkb
        else:
            # Custom auth: consult ALLOWED_EXTRA_AUTHENTICATIONS config
            # (1:1 with superset_old: app.config["ALLOWED_EXTRA_AUTHENTICATIONS"]
            # -> SupersetSettings().allowed_extra_authentications).
            from superset.config import SupersetSettings

            _settings = SupersetSettings()  # type: ignore[call-arg]
            allowed_extra_auths: dict[str, Any] = (
                _settings.allowed_extra_authentications.get("snowflake", {})
            )
            if auth_method in allowed_extra_auths:
                snowflake_auth = allowed_extra_auths.get(auth_method)
            else:
                raise ValueError(
                    f"For security reason, custom authentication '{auth_method}' "
                    f"must be listed in 'ALLOWED_EXTRA_AUTHENTICATIONS' config"
                )
            connect_args["auth"] = snowflake_auth(**auth_params)


__all__ = [
    "SnowflakeEngineSpec",
]
