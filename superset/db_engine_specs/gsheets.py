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

import re
from re import Pattern
from typing import Any

from superset.db_engine_specs.shillelagh import ShillelaghEngineSpec
from superset.errors import SupersetErrorType

SYNTAX_ERROR_REGEX = re.compile('SQLError: near "(?P<server_error>.*?)": syntax error')

# The shillelagh google-sheets adapter raises this exception when the access
# token is missing / expired.  ``shillelagh`` is an optional dependency, so
# fall back to a sentinel (matches BaseEngineSpec.oauth2_exception default
# behaviour: never matches anything).
try:
    from shillelagh.exceptions import (  # type: ignore[import-not-found]
        UnauthenticatedError as _UnauthenticatedError,
    )
except ImportError:  # pragma: no cover -- optional dep
    _UnauthenticatedError = type("_NoUnauthenticatedError", (BaseException,), {})


class GSheetsEngineSpec(ShillelaghEngineSpec):
    """Engine for Google spreadsheets"""

    engine_name = "Google Sheets"
    engine = "gsheets"
    allows_joins = True
    allows_subqueries = True

    default_driver = "apsw"
    sqlalchemy_uri_placeholder = "gsheets://"

    # when editing the database, mask this field in `encrypted_extra`
    encrypted_extra_sensitive_fields = {"$.service_account_info.private_key"}

    custom_errors: dict[Pattern[str], tuple[str, SupersetErrorType, dict[str, Any]]] = {
        SYNTAX_ERROR_REGEX: (
            'Please check your query for syntax errors near "%(server_error)s". '
            "Then, try running your query again.",
            SupersetErrorType.SYNTAX_ERROR,
            {},
        ),
    }

    supports_file_upload = True

    # OAuth 2.0
    # Mirrors ``superset_old/db_engine_specs/gsheets.py``.
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
    def build_sqlalchemy_uri(
        cls,
        _: dict[str, Any],
        encrypted_extra: dict[str, Any] | None = None,
    ) -> str:
        if encrypted_extra and "oauth2_client_info" in encrypted_extra:
            del encrypted_extra["oauth2_client_info"]

        return "gsheets://"

    @classmethod
    def get_parameters_from_uri(
        cls,
        uri: str,
        encrypted_extra: dict[str, Any] | None = None,
    ) -> Any:
        if encrypted_extra:
            return {**encrypted_extra}

        raise ValueError("Invalid service credentials")
