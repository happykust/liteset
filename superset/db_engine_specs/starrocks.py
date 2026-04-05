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
"""StarRocks engine spec -- sync/Flask-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/starrocks.py`` with Flask
imports removed.  Only overridden methods and attributes are included.
"""

from __future__ import annotations

import re
from re import Pattern
from typing import Any
from urllib import parse

from sqlalchemy import Float, Integer, Numeric, types
from sqlalchemy.engine.url import URL
from sqlalchemy.sql.type_api import TypeEngine

from superset.db_engine_specs.mysql import MySQLEngineSpec
from superset.typing import GenericDataType

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


class PERCENTILE(Numeric):
    __visit_name__ = "PERCENTILE"


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


# ---------------------------------------------------------------------------
# Regular expressions to catch custom errors
# ---------------------------------------------------------------------------

CONNECTION_ACCESS_DENIED_REGEX = re.compile(
    "Access denied for user '(?P<username>.*?)'"
)
CONNECTION_UNKNOWN_DATABASE_REGEX = re.compile(
    "Unknown database '(?P<database>.*?)'"
)


class StarRocksEngineSpec(MySQLEngineSpec):
    engine = "starrocks"
    engine_name = "StarRocks"

    default_driver = "starrocks"
    sqlalchemy_uri_placeholder = (
        "starrocks://user:password@host:port/catalog.db[?key=value&key=value...]"
    )

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
            re.compile(r"^json", re.IGNORECASE),
            types.JSON(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^binary.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^percentile", re.IGNORECASE),
            PERCENTILE(),
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
    )

    custom_errors: dict[Pattern[str], tuple[str, str, dict[str, Any]]] = {
        CONNECTION_ACCESS_DENIED_REGEX: (
            'Either the username "%(username)s" or the password is incorrect.',
            "CONNECTION_ACCESS_DENIED_ERROR",
            {"invalid": ["username", "password"]},
        ),
        CONNECTION_UNKNOWN_DATABASE_REGEX: (
            'Unable to connect to database "%(database)s".',
            "CONNECTION_UNKNOWN_DATABASE_ERROR",
            {"invalid": ["database"]},
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
        database = uri.database
        if schema and database:
            schema = parse.quote(schema, safe="")
            if "." in database:
                database = database.split(".")[0] + "." + schema
            else:
                database = "default_catalog." + schema
            uri = uri.set(database=database)

        return uri, connect_args

    @classmethod
    def get_schema_from_engine_params(
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> str | None:
        database = sqlalchemy_uri.database.strip("/")

        if "." not in database:
            return None

        return parse.unquote(database.split(".")[1])


__all__ = [
    "StarRocksEngineSpec",
]
