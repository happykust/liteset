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

# ---------------------------------------------------------------------------
# The shillelagh google-sheets adapter raises this exception when the access
# token is missing / expired.  ``shillelagh`` is an optional dependency, so
# fall back to a sentinel (matches BaseEngineSpec.oauth2_exception default
# behaviour: never matches anything).
# ---------------------------------------------------------------------------
try:
    from shillelagh.exceptions import (  # type: ignore[import-not-found]
        UnauthenticatedError as _UnauthenticatedError,
    )
except ImportError:  # pragma: no cover -- optional dep
    _UnauthenticatedError = type("_NoUnauthenticatedError", (BaseException,), {})

# ---------------------------------------------------------------------------
# security_manager proxy
# ---------------------------------------------------------------------------
# The original imports ``security_manager`` from ``superset`` which is a
# Flask-AppBuilder ``LocalProxy``; it provides ``find_user(username=...)``.
# In the port there is no Flask-AppBuilder.  We expose a thin proxy so:
#   1. Test fixtures can patch ``superset.db_engine_specs.gsheets.security_manager``
#      exactly as the upstream tests do.
#   2. ``impersonate_user`` can call ``security_manager.find_user(username=u)``.
# ---------------------------------------------------------------------------


class _SecurityManagerProxy:  # pylint: disable=too-few-public-methods
    """Minimal proxy that mirrors the ``find_user`` interface used here."""

    @staticmethod
    def find_user(username: str | None = None) -> Any | None:  # noqa: ARG004
        """Return the User row whose ``username`` column matches, or *None*.

        Uses a synchronous SQLAlchemy session (psycopg2 / sqlite) so this is
        safe to call from synchronous code (Celery tasks, sync engine-spec
        callbacks).
        """
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

# ---------------------------------------------------------------------------
# GSheets parameters JSON schema (replaces Marshmallow GSheetsParametersSchema)
# ---------------------------------------------------------------------------
# The original uses ``GSheetsParametersSchema`` (a Marshmallow Schema subclass)
# together with ``APISpec`` + ``MarshmallowPlugin`` to auto-generate an OpenAPI
# component schema.  The port does not ship Marshmallow; ``parameters_schema``
# is a plain JSON Schema dict and ``parameters_json_schema()`` in the base spec
# just returns it as-is (superset/db_engine_specs/base.py lines 2360-2367).
# The shape below matches what the Marshmallow auto-generation would produce.
# ---------------------------------------------------------------------------
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
    """Engine for Google spreadsheets"""

    engine_name = "Google Sheets"
    engine = "gsheets"
    allows_joins = True
    allows_subqueries = True

    parameters_schema = GSHEETS_PARAMETERS_JSON_SCHEMA
    default_driver = "apsw"
    sqlalchemy_uri_placeholder = "gsheets://"

    # when editing the database, mask this field in `encrypted_extra`
    # pylint: disable=invalid-name
    encrypted_extra_sensitive_fields = {"$.service_account_info.private_key"}

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

    # OAuth 2.0 — mirrors ``superset_old/db_engine_specs/gsheets.py``
    # SCOPES is kept as a class attribute so callers can reference it; the
    # individual string values come from ``shillelagh.adapters.api.gsheets.lib``
    # in the original; we hard-code them here since shillelagh is optional.
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
        """Set the ``subject`` / ``access_token`` query-params for impersonation.

        1:1 with ``superset_old/db_engine_specs/gsheets.py``.
        Flask ``security_manager`` → module-level ``security_manager`` proxy.
        """
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
        """Return the ``GET_METADATA`` response for a GSheets table.

        1:1 with ``superset_old/db_engine_specs/gsheets.py``.
        Uses ``database.get_raw_connection`` exactly as the original does.
        """
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
        """Remove ``oauth2_client_info`` from ``encrypted_extra``.

        1:1 with ``superset_old/db_engine_specs/gsheets.py``.
        Calls the parent implementation first (populates *params* from
        ``database.encrypted_extra``) then strips ``oauth2_client_info``.
        """
        ShillelaghEngineSpec.update_params_from_encrypted_extra(database, params)

        if "oauth2_client_info" in params:
            del params["oauth2_client_info"]

    @classmethod
    def get_parameters_from_uri(
        cls,
        uri: str,  # pylint: disable=unused-argument
        encrypted_extra: dict[str, Any] | None = None,
    ) -> Any:
        # Building parameters from encrypted_extra and uri
        if encrypted_extra:
            return {**encrypted_extra}

        raise ValueError("Invalid service credentials")

    @classmethod
    def parameters_json_schema(cls) -> Any:
        """Return configuration parameters as OpenAPI JSON Schema.

        1:1 with ``superset_old/db_engine_specs/gsheets.py`` in *effect*:
        the original used ``APISpec`` + ``MarshmallowPlugin`` to auto-generate
        the schema; the port stores the equivalent JSON Schema dict directly
        as ``parameters_schema`` and returns it here.
        """
        return cls.parameters_schema or None

    @classmethod
    def validate_parameters(
        cls,
        properties: GSheetsPropertiesType,
    ) -> list[SupersetError]:
        """Validate catalog entries by probing each sheet URL.

        1:1 with ``superset_old/db_engine_specs/gsheets.py``.
        Flask ``g.user`` → the port's ContextVar-based
        ``superset.utils.core.get_current_user`` (Flask is removed).
        ``from flask_babel import gettext as __`` → plain strings (no i18n
        dependency in the port).
        """
        from superset.utils.core import get_current_user

        errors: list[SupersetError] = []

        # backwards compatible just incase people are send data
        # via parameters for validation
        parameters = properties.get("parameters", {})
        if parameters and parameters.get("catalog"):
            table_catalog = parameters.get("catalog") or {}
        else:
            table_catalog = properties.get("catalog") or {}

        encrypted_credentials = parameters.get("service_account_info") or "{}"

        # On create the encrypted credentials are a string,
        # at all other times they are a dict
        if isinstance(encrypted_credentials, str):
            encrypted_credentials = json.loads(encrypted_credentials)

        # We need a subject in case domain wide delegation is set, otherwise the
        # check will fail. This means that the admin will be able to add sheets
        # that only they have access, even if later users are not able to access
        # them.
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
        """POST to the Google API.

        Helper function that handles logging and error handling.
        1:1 with ``superset_old/db_engine_specs/gsheets.py``.
        """
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
        """Create a new sheet and update the DB catalog.

        Since Google Sheets is not a database, uploading a file is slightly
        different from other traditional databases.  To create a table with a
        given name we first create a spreadsheet with the contents of the
        dataframe, and we later update the database catalog to add a mapping
        between the desired table name and the URL of the new sheet.

        If the table already exists and the user wants it replaced we clear
        all the cells in the existing sheet before uploading the new data.
        Appending to an existing table is not supported because we can't
        ensure that the schemas match.

        1:1 with ``superset_old/db_engine_specs/gsheets.py``.
        ``cls.get_engine(database, ...)`` → ``database.get_sqla_engine(...)``
        (the original's ``get_engine`` is a thin wrapper over ``get_sqla_engine``).
        ``db.session.add/commit`` → a single sync session (see below).
        """
        # pylint: disable=import-outside-toplevel
        from shillelagh.backends.apsw.dialects.base import get_adapter_for_table_name

        # grab the existing catalog, if any
        extra = database.get_extra()
        engine_params = extra.setdefault("engine_params", {})
        catalog = engine_params.setdefault("catalog", {})

        # sanity checks
        spreadsheet_url = catalog.get(table.table)
        if spreadsheet_url and "if_exists" in to_sql_kwargs:
            if to_sql_kwargs["if_exists"] == "append":
                # no way we're going to append a dataframe to a spreadsheet, that's
                # never going to work
                raise SupersetException("Append operation not currently supported")
            if to_sql_kwargs["if_exists"] == "fail":
                raise SupersetException("Table already exists")
            if to_sql_kwargs["if_exists"] == "replace":
                pass

        # get the Google session from the Shillelagh adapter
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

        # clear existing sheet, or create a new one
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

        # insert data
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

        # update catalog
        catalog[table.table] = spreadsheet_url
        database.extra = json.dumps(extra)
        # The original ``db.session.add/commit`` relies on Flask-SQLAlchemy's
        # scoped session being the one ``database`` lives in. The port's
        # ``get_sync_session()`` returns a NEW session per call, so add + commit
        # MUST run on a single session (the instance's own when bound, else a
        # fresh sync session) — otherwise the catalog update never persists.
        from sqlalchemy.orm import object_session

        from superset.db.session import get_sync_session

        session = object_session(database) or get_sync_session()
        session.add(database)
        session.commit()  # pylint: disable=consider-using-transaction
