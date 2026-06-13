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
"""Apache Doris engine spec -- sync-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/doris.py`` with legacy
imports removed.  Only overridden methods and attributes are included.
"""

from __future__ import annotations

import logging
import re
from re import Pattern
from typing import Any, TYPE_CHECKING
from urllib import parse

from sqlalchemy import Float, Integer, Numeric, String, TEXT, types
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import URL
from sqlalchemy.sql.type_api import TypeEngine

from superset.db_engine_specs.mysql import MySQLEngineSpec
from superset.typing import GenericDataType

if TYPE_CHECKING:
    from superset.models.core import Database

DEFAULT_CATALOG = "internal"
DEFAULT_SCHEMA = "information_schema"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regular expressions to catch custom errors
# ---------------------------------------------------------------------------

CONNECTION_ACCESS_DENIED_REGEX = re.compile(
    "Access denied for user '(?P<username>.*?)'"
)
CONNECTION_INVALID_HOSTNAME_REGEX = re.compile(
    "Unknown Doris server host '(?P<hostname>.*?)'"
)
CONNECTION_UNKNOWN_DATABASE_REGEX = re.compile("Unknown database '(?P<database>.*?)'")
CONNECTION_HOST_DOWN_REGEX = re.compile(
    "Can't connect to Doris server on '(?P<hostname>.*?)'"
)
SYNTAX_ERROR_REGEX = re.compile(
    "check the manual that corresponds to your MySQL server "
    "version for the right syntax to use near '(?P<server_error>.*)"
)


# ---------------------------------------------------------------------------
# Custom types
# ---------------------------------------------------------------------------


class TINYINT(Integer):
    __visit_name__ = "TINYINT"


class LARGEINT(Integer):
    __visit_name__ = "LARGEINT"


class DOUBLE(Float):
    __visit_name__ = "DOUBLE"


class HLL(Numeric):
    __visit_name__ = "HLL"


class BITMAP(Numeric):
    __visit_name__ = "BITMAP"


class QuantileState(Numeric):
    __visit_name__ = "QUANTILE_STATE"


class AggState(Numeric):
    __visit_name__ = "AGG_STATE"


class ARRAY(TypeEngine):
    __visit_name__ = "ARRAY"

    @property
    def python_type(self) -> type[list[Any]] | None:
        return list


class MAP(TypeEngine):
    __visit_name__ = "MAP"

    @property
    def python_type(self) -> type[dict[Any, Any]] | None:
        return dict


class STRUCT(TypeEngine):
    __visit_name__ = "STRUCT"

    @property
    def python_type(self) -> type[Any] | None:
        return None


class DorisEngineSpec(MySQLEngineSpec):
    engine = "pydoris"
    engine_aliases = {"doris"}
    engine_name = "Apache Doris"
    max_column_name_length = 64
    default_driver = "pydoris"
    sqlalchemy_uri_placeholder = (
        "doris://user:password@host:port/catalog.db[?key=value&key=value...]"
    )
    encryption_parameters = {"ssl": "0"}
    supports_dynamic_schema = True
    supports_catalog = True
    supports_dynamic_catalog = True
    supports_cross_catalog_queries = False

    column_type_mappings = (  # type: ignore
        (
            re.compile(r"^tinyint", re.IGNORECASE),
            TINYINT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^largeint", re.IGNORECASE),
            LARGEINT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^decimal.*", re.IGNORECASE),
            types.DECIMAL(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^double", re.IGNORECASE),
            DOUBLE(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^varchar(\((\d+)\))*$", re.IGNORECASE),
            types.VARCHAR(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^char(\((\d+)\))*$", re.IGNORECASE),
            types.CHAR(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^json.*", re.IGNORECASE),
            types.JSON(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^binary.*", re.IGNORECASE),
            types.BINARY(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^quantile_state", re.IGNORECASE),
            QuantileState(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^agg_state.*", re.IGNORECASE),
            AggState(),
            GenericDataType.STRING,
        ),
        (re.compile(r"^hll", re.IGNORECASE), HLL(), GenericDataType.STRING),
        (
            re.compile(r"^bitmap", re.IGNORECASE),
            BITMAP(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^array.*", re.IGNORECASE),
            ARRAY(),
            GenericDataType.STRING,
        ),
        (re.compile(r"^map.*", re.IGNORECASE), MAP(), GenericDataType.STRING),
        (
            re.compile(r"^struct.*", re.IGNORECASE),
            STRUCT(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^datetime.*", re.IGNORECASE),
            types.DATETIME(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^date.*", re.IGNORECASE),
            types.DATE(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^text.*", re.IGNORECASE),
            TEXT(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^string.*", re.IGNORECASE),
            String(),
            GenericDataType.STRING,
        ),
    )

    custom_errors: dict[Pattern[str], tuple[str, str, dict[str, Any]]] = {
        CONNECTION_ACCESS_DENIED_REGEX: (
            'Either the username "%(username)s" or the password is incorrect.',
            "CONNECTION_ACCESS_DENIED_ERROR",
            {"invalid": ["username", "password"]},
        ),
        CONNECTION_INVALID_HOSTNAME_REGEX: (
            'Unknown Doris server host "%(hostname)s".',
            "CONNECTION_INVALID_HOSTNAME_ERROR",
            {"invalid": ["host"]},
        ),
        CONNECTION_HOST_DOWN_REGEX: (
            'The host "%(hostname)s" might be down and can\'t be reached.',
            "CONNECTION_HOST_DOWN_ERROR",
            {"invalid": ["host", "port"]},
        ),
        CONNECTION_UNKNOWN_DATABASE_REGEX: (
            'Unable to connect to database "%(database)s".',
            "CONNECTION_UNKNOWN_DATABASE_ERROR",
            {"invalid": ["database"]},
        ),
        SYNTAX_ERROR_REGEX: (
            'Please check your query for syntax errors near "%(server_error)s". '
            "Then, try running your query again.",
            "SYNTAX_ERROR",
            {},
        ),
    }

    @classmethod
    def adjust_engine_params(
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        if not uri.database:
            raise ValueError("Doris requires a database to be specified in the URI.")
        elif "." not in uri.database:
            current_catalog, current_schema = None, uri.database
        else:
            current_catalog, current_schema = uri.database.split(".", 1)

        catalog = catalog or current_catalog
        schema = schema or current_schema

        database = ".".join(part for part in (catalog, schema) if part)
        uri = uri.set(database=database)

        return uri, connect_args

    @classmethod
    def get_default_catalog(cls, database: Database) -> str:
        # first check the URI to see if a default catalog is set
        if database.url_object.database and "." in database.url_object.database:
            return database.url_object.database.split(".")[0]

        # if not, iterate over existing catalogs and find the current one —
        # 1:1 with upstream (``SHOW CATALOGS`` → row where ``IsCurrent``).
        try:
            with database.get_sqla_engine() as engine:
                for catalog in engine.execute("SHOW CATALOGS"):
                    if catalog.IsCurrent:
                        return catalog.CatalogName
        except Exception:  # noqa: BLE001 — fall back if the probe query fails
            logger.warning("Could not resolve current Doris catalog", exc_info=True)

        # fallback to "internal"
        return DEFAULT_CATALOG

    @classmethod
    def get_catalog_names(
        cls,
        database: Database,
        inspector: Inspector,
    ) -> set[str]:
        result = inspector.bind.execute("SHOW CATALOGS")
        return {row.CatalogName for row in result}

    @classmethod
    def get_schema_from_engine_params(
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> str | None:
        if not sqlalchemy_uri.database:
            return None

        schema = sqlalchemy_uri.database.split(".")[-1].strip("/")
        return parse.unquote(schema)


__all__ = [
    "DorisEngineSpec",
]
