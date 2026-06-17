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

from __future__ import annotations

import logging
import re
from re import Pattern
from typing import Any, TYPE_CHECKING, TypedDict

import pandas as pd
from sqlalchemy.engine import create_engine
from sqlalchemy.engine.url import URL

from superset.db_engine_specs.shillelagh import ShillelaghEngineSpec
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import SupersetException
from superset.utils import json

if TYPE_CHECKING:
    from requests import Session

    from superset.models.core import Database
    from superset.sql.parse import Table

_logger = logging.getLogger(__name__)

EXAMPLE_GSHEETS_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1LcWZMsdCl92g7nA-D6qGRqg1T5TiHyuKJUY1u9XAnsk/edit#gid=0"
)

SYNTAX_ERROR_REGEX = re.compile('SQLError: near "(?P<server_error>.*?)": syntax error')

# shillelagh raises UnauthenticatedError when the OAuth2 token is missing/expired.
# It's optional, so fall back to a sentinel that never matches (same as base default).
try:
    from shillelagh.exceptions import (  # type: ignore[import-not-found]
        UnauthenticatedError as _UnauthenticatedError,
    )
except ImportError:  # pragma: no cover -- optional dep
    _UnauthenticatedError = type("_NoUnauthenticatedError", (BaseException,), {})


# Thin proxy so test fixtures can patch gsheets.security_manager exactly as upstream
# tests do, and impersonate_user can call security_manager.find_user(username=u).
class _SecurityManagerProxy:  # pylint: disable=too-few-public-methods
    @staticmethod
    def find_user(username: str | None = None) -> Any | None:  # noqa: ARG004
        """Look up a User by username using a sync session (safe for Celery tasks)."""
        if not username:
            return None
        from superset.db.session import get_sync_session
        from superset.models.security import User

        session = get_sync_session()
        try:
            return session.query(User).filter(User.username == username).one_or_none()
        finally:
            session.close()


security_manager = _SecurityManagerProxy()

GSHEETS_PARAMETERS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "catalog": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "service_account_info": {
            "type": "string",
            "description": "Contents of GSheets JSON credentials.",
            "x-encrypted-extra": True,
        },
        "oauth2_client_info": {
            "type": "string",
            "nullable": True,
            "description": "OAuth2 client information",
            "x-encrypted-extra": True,
        },
    },
}


class GSheetsParametersType(TypedDict):
    service_account_info: str
    catalog: dict[str, str] | None


class GSheetsPropertiesType(TypedDict):
    parameters: GSheetsParametersType
    catalog: dict[str, str]


class GSheetsEngineSpec(ShillelaghEngineSpec):
    engine_name = "Google Sheets"
    engine = "gsheets"
    allows_joins = True
    allows_subqueries = True

    parameters_schema = GSHEETS_PARAMETERS_JSON_SCHEMA
    default_driver = "apsw"
    sqlalchemy_uri_placeholder = "gsheets://"

    encrypted_extra_sensitive_fields = {"$.service_account_info.private_key"}  # pylint: disable=invalid-name

    custom_errors: dict[Pattern[str], tuple[str, SupersetErrorType, dict[str, Any]]] = {
        SYNTAX_ERROR_REGEX: (
            (
                'Please check your query for syntax errors near "%(server_error)s". '
                "Then, try running your query again."
            ),
            SupersetErrorType.SYNTAX_ERROR,
            {},
        ),
    }

    supports_file_upload = True

    # Hard-coded because shillelagh is optional; originally from
    # shillelagh.adapters.api.gsheets.lib.
    SCOPES = (
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://spreadsheets.google.com/feeds",
    )
    supports_oauth2 = True
    oauth2_scope = " ".join(SCOPES)
    oauth2_authorization_request_uri = "https://accounts.google.com/o/oauth2/v2/auth"
    oauth2_token_request_uri = "https://oauth2.googleapis.com/token"  # noqa: S105
    oauth2_exception = _UnauthenticatedError

    @classmethod
    def impersonate_user(
        cls,
        database: Database,
        username: str | None,
        user_token: str | None,
        url: URL,
        engine_kwargs: dict[str, Any],
    ) -> tuple[URL, dict[str, Any]]:
        if username is not None:
            user = security_manager.find_user(username=username)
            if user and user.email:
                url = url.update_query_dict({"subject": user.email})

        if user_token:
            url = url.update_query_dict({"access_token": user_token})

        return url, engine_kwargs

    @classmethod
    def get_extra_table_metadata(
        cls,
        database: Database,
        table: Table,
    ) -> dict[str, Any]:
        with database.get_raw_connection(
            catalog=table.catalog,
            schema=table.schema,
        ) as conn:
            cursor = conn.cursor()
            cursor.execute(f'SELECT GET_METADATA("{table.table}")')
            results = cursor.fetchone()[0]
        try:
            metadata = json.loads(results)
        except Exception:  # pylint: disable=broad-except
            metadata = {}

        return {"metadata": metadata["extra"]}

    @classmethod
    # pylint: disable=unused-argument
    def build_sqlalchemy_uri(
        cls,
        _: GSheetsParametersType,
        encrypted_extra: None | (dict[str, Any]) = None,
    ) -> str:
        if encrypted_extra and "oauth2_client_info" in encrypted_extra:
            del encrypted_extra["oauth2_client_info"]

        return "gsheets://"

    @staticmethod
    def update_params_from_encrypted_extra(
        database: Database,
        params: dict[str, Any],
    ) -> None:
        # Call parent first, then strip oauth2_client_info (not a connect arg).

        ShillelaghEngineSpec.update_params_from_encrypted_extra(database, params)

        if "oauth2_client_info" in params:
            del params["oauth2_client_info"]

    @classmethod
    def get_parameters_from_uri(
        cls,
        uri: str,  # pylint: disable=unused-argument
        encrypted_extra: dict[str, Any] | None = None,
    ) -> Any:
        if encrypted_extra:
            return {**encrypted_extra}

        raise ValueError("Invalid service credentials")

    @classmethod
    def parameters_json_schema(cls) -> Any:
        return cls.parameters_schema or None

    @classmethod
    def validate_parameters(
        cls,
        properties: GSheetsPropertiesType,
    ) -> list[SupersetError]:
        """Probe each catalog URL to validate sheet access."""
        from superset.utils.core import get_current_user

        errors: list[SupersetError] = []

        parameters = properties.get("parameters", {})
        if parameters and parameters.get("catalog"):
            table_catalog = parameters.get("catalog") or {}
        else:
            table_catalog = properties.get("catalog") or {}

        encrypted_credentials = parameters.get("service_account_info") or "{}"

        if isinstance(encrypted_credentials, str):
            encrypted_credentials = json.loads(encrypted_credentials)

        # Subject needed for domain-wide delegation; admin can add sheets others
        # might not access later.
        current_user = get_current_user()
        subject = current_user.email if current_user else None

        engine = create_engine(
            "gsheets://",
            service_account_info=encrypted_credentials,
            subject=subject,
        )
        conn = engine.connect()
        idx = 0

        for name, url in table_catalog.items():
            if not name:
                errors.append(
                    SupersetError(
                        message="Sheet name is required",
                        error_type=SupersetErrorType.CONNECTION_MISSING_PARAMETERS_ERROR,
                        level=ErrorLevel.WARNING,
                        extra={"catalog": {"idx": idx, "name": True}},
                    ),
                )
                return errors

            if not url:
                errors.append(
                    SupersetError(
                        message="URL is required",
                        error_type=SupersetErrorType.CONNECTION_MISSING_PARAMETERS_ERROR,
                        level=ErrorLevel.WARNING,
                        extra={"catalog": {"idx": idx, "url": True}},
                    ),
                )
                return errors

            try:
                results = conn.execute(f'SELECT * FROM "{url}" LIMIT 1')  # noqa: S608
                results.fetchall()
            except Exception:  # pylint: disable=broad-except
                errors.append(
                    SupersetError(
                        message=(
                            "The URL could not be identified. Please check for typos "
                            "and make sure that 'Type of Google Sheets allowed' "
                            "selection matches the input."
                        ),
                        error_type=SupersetErrorType.TABLE_DOES_NOT_EXIST_ERROR,
                        level=ErrorLevel.WARNING,
                        extra={"catalog": {"idx": idx, "url": True}},
                    ),
                )
            idx += 1
        return errors

    @staticmethod
    def _do_post(
        session: Session,  # pylint: disable=disallowed-name
        url: str,
        body: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        _logger.info("POST %s", url)
        _logger.debug(body)
        response = session.post(
            url,
            json=body,
            **kwargs,
        )

        payload = response.json()
        _logger.debug(payload)

        if "error" in payload:
            raise SupersetException(payload["error"]["message"])

        return payload

    @classmethod
    def df_to_sql(  # pylint: disable=too-many-locals
        cls,
        database: Database,
        table: Table,
        df: pd.DataFrame,
        to_sql_kwargs: dict[str, Any],
    ) -> None:
        """Create a new GSheets spreadsheet and register it in the DB catalog.

        Replace clears the existing sheet; append is not supported
        (schema mismatch risk).
        """
        # pylint: disable=import-outside-toplevel
        from shillelagh.backends.apsw.dialects.base import get_adapter_for_table_name

        extra = database.get_extra()
        engine_params = extra.setdefault("engine_params", {})
        catalog = engine_params.setdefault("catalog", {})

        spreadsheet_url = catalog.get(table.table)
        if spreadsheet_url and "if_exists" in to_sql_kwargs:
            if to_sql_kwargs["if_exists"] == "append":
                raise SupersetException("Append operation not currently supported")
            if to_sql_kwargs["if_exists"] == "fail":
                raise SupersetException("Table already exists")
            if to_sql_kwargs["if_exists"] == "replace":
                pass

        with database.get_sqla_engine(
            catalog=table.catalog,
            schema=table.schema,
        ) as engine:
            with engine.connect() as conn:
                # any GSheets URL will work to get a working session
                adapter = get_adapter_for_table_name(
                    conn,
                    spreadsheet_url or EXAMPLE_GSHEETS_URL,
                )
                session = (  # pylint: disable=disallowed-name
                    adapter._get_session()  # pylint: disable=protected-access
                )

        if spreadsheet_url:
            spreadsheet_id = adapter._spreadsheet_id  # pylint: disable=protected-access
            range_ = adapter._sheet_name  # pylint: disable=protected-access
            url = (
                "https://sheets.googleapis.com/v4/spreadsheets/"
                f"{spreadsheet_id}/values/{range_}:clear"
            )
            cls._do_post(session, url, {})
        else:
            payload = cls._do_post(
                session,
                "https://sheets.googleapis.com/v4/spreadsheets",
                {"properties": {"title": table.table}},
            )
            spreadsheet_id = payload["spreadsheetId"]
            range_ = payload["sheets"][0]["properties"]["title"]
            spreadsheet_url = payload["spreadsheetUrl"]

        data = df.fillna("").values.tolist()
        data.insert(0, df.columns.values.tolist())
        body = {
            "range": range_,
            "majorDimension": "ROWS",
            "values": data,
        }
        url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{spreadsheet_id}/values/{range_}:append"
        )
        cls._do_post(
            session,
            url,
            body,
            params={"valueInputOption": "USER_ENTERED"},
        )

        catalog[table.table] = spreadsheet_url
        database.extra = json.dumps(extra)
        # get_sync_session() returns a NEW session per call; commit must run on
        # the instance's own session (when bound) to avoid losing the catalog update.
        from sqlalchemy.orm import object_session

        from superset.db.session import get_sync_session

        session = object_session(database) or get_sync_session()
        session.add(database)
        session.commit()  # pylint: disable=consider-using-transaction
