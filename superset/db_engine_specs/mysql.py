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
"""MySQL database engine spec."""

from __future__ import annotations

import contextlib
import re
from datetime import datetime
from decimal import Decimal
from re import Pattern
from typing import Any, Callable, TYPE_CHECKING
from urllib import parse

from sqlalchemy import types
from sqlalchemy.dialects.mysql import (
    BIT,
    DECIMAL,
    DOUBLE,
    FLOAT,
    INTEGER,
    LONGTEXT,
    MEDIUMINT,
    MEDIUMTEXT,
    TINYINT,
    TINYTEXT,
)
from sqlalchemy.engine.url import URL

from superset.constants import TimeGrain
from superset.db_engine_specs.base import (
    BaseEngineSpec,
    BasicParametersMixin,
    ColumnTypeMapping,
)
from superset.typing import GenericDataType

if TYPE_CHECKING:
    from superset.models.sql_lab import Query

CONNECTION_ACCESS_DENIED_REGEX = re.compile(
    "Access denied for user '(?P<username>.*?)'@'(?P<hostname>.*?)'"
)
CONNECTION_INVALID_HOSTNAME_REGEX = re.compile(
    "Unknown MySQL server host '(?P<hostname>.*?)'"
)
CONNECTION_HOST_DOWN_REGEX = re.compile(
    "Can't connect to MySQL server on '(?P<hostname>.*?)'"
)
CONNECTION_UNKNOWN_DATABASE_REGEX = re.compile("Unknown database '(?P<database>.*?)'")

SYNTAX_ERROR_REGEX = re.compile(
    "check the manual that corresponds to your MySQL server "
    "version for the right syntax to use near '(?P<server_error>.*)"
)


class MySQLEngineSpec(BasicParametersMixin, BaseEngineSpec):
    engine = "mysql"
    engine_name = "MySQL"
    max_column_name_length = 64

    default_driver = "mysqldb"
    sqlalchemy_uri_placeholder = (
        "mysql://user:password@host:port/dbname[?key=value&key=value...]"
    )
    encryption_parameters = {"ssl": "1"}

    supports_dynamic_schema = True

    column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r"^int.*", re.IGNORECASE),
            INTEGER(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^tinyint", re.IGNORECASE),
            TINYINT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^mediumint", re.IGNORECASE),
            MEDIUMINT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^decimal", re.IGNORECASE),
            DECIMAL(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^float", re.IGNORECASE),
            FLOAT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^double", re.IGNORECASE),
            DOUBLE(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bit", re.IGNORECASE),
            BIT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^tinytext", re.IGNORECASE),
            TINYTEXT(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^mediumtext", re.IGNORECASE),
            MEDIUMTEXT(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^longtext", re.IGNORECASE),
            LONGTEXT(),
            GenericDataType.STRING,
        ),
    )
    column_type_mutators: dict[types.TypeEngine, Callable[[Any], Any]] = {
        DECIMAL: lambda val: Decimal(val) if isinstance(val, str) else val
    }

    _time_grain_expressions = {
        None: "{col}",
        TimeGrain.SECOND: (
            "DATE_ADD(DATE({col}), "
            "INTERVAL (HOUR({col})*60*60 + MINUTE({col})*60"
            " + SECOND({col})) SECOND)"
        ),
        TimeGrain.MINUTE: (
            "DATE_ADD(DATE({col}), INTERVAL (HOUR({col})*60 + MINUTE({col})) MINUTE)"
        ),
        TimeGrain.HOUR: "DATE_ADD(DATE({col}), INTERVAL HOUR({col}) HOUR)",
        TimeGrain.DAY: "DATE({col})",
        TimeGrain.WEEK: "DATE(DATE_SUB({col}, INTERVAL DAYOFWEEK({col}) - 1 DAY))",
        TimeGrain.MONTH: "DATE(DATE_SUB({col}, INTERVAL DAYOFMONTH({col}) - 1 DAY))",
        TimeGrain.QUARTER: (
            "MAKEDATE(YEAR({col}), 1) "
            "+ INTERVAL QUARTER({col}) QUARTER - INTERVAL 1 QUARTER"
        ),
        TimeGrain.YEAR: "DATE(DATE_SUB({col}, INTERVAL DAYOFYEAR({col}) - 1 DAY))",
        TimeGrain.WEEK_STARTING_MONDAY: (
            "DATE(DATE_SUB({col}, "
            "INTERVAL DAYOFWEEK(DATE_SUB({col}, "
            "INTERVAL 1 DAY)) - 1 DAY))"
        ),
    }

    type_code_map: dict[int, str] = {}  # loaded from get_datatype only if needed

    custom_errors: dict[Pattern[str], tuple[str, str, dict[str, Any]]] = {
        CONNECTION_ACCESS_DENIED_REGEX: (
            'Either the username "%(username)s" or the password is incorrect.',
            "CONNECTION_ACCESS_DENIED_ERROR",
            {"invalid": ["username", "password"]},
        ),
        CONNECTION_INVALID_HOSTNAME_REGEX: (
            'Unknown MySQL server host "%(hostname)s".',
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
    disallow_uri_query_params = {
        "mysqldb": {"local_infile"},
        "mysqlconnector": {"allow_local_infile"},
    }
    enforce_uri_query_params = {
        "mysqldb": {"local_infile": 0},
        "mysqlconnector": {"allow_local_infile": 0},
    }

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"STR_TO_DATE('{dttm.date().isoformat()}', '%Y-%m-%d')"
        if isinstance(sqla_type, types.DateTime):
            datetime_formatted = dttm.isoformat(sep=" ", timespec="microseconds")
            return f"""STR_TO_DATE('{datetime_formatted}', '%Y-%m-%d %H:%i:%s.%f')"""
        return None

    @classmethod
    def adjust_engine_params(
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        uri, new_connect_args = super().adjust_engine_params(
            uri,
            connect_args,
            catalog,
            schema,
        )

        if schema:
            uri = uri.set(database=parse.quote(schema, safe=""))

        return uri, new_connect_args

    @classmethod
    def get_schema_from_engine_params(
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> str | None:
        # A MySQL database maps 1:1 to a SQLAlchemy schema.
        return parse.unquote(sqlalchemy_uri.database)

    @classmethod
    def get_datatype(cls, type_code: Any) -> str | None:
        if not cls.type_code_map:
            import MySQLdb  # noqa: I001

            ft = MySQLdb.constants.FIELD_TYPE
            cls.type_code_map = {
                getattr(ft, k): k for k in dir(ft) if not k.startswith("_")
            }
        datatype = type_code
        if isinstance(type_code, int):
            datatype = cls.type_code_map.get(type_code)
        if datatype and isinstance(datatype, str) and datatype:
            return datatype
        return None

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "from_unixtime({col})"

    @classmethod
    def _extract_error_message(cls, ex: Exception) -> str:
        message = str(ex)
        with contextlib.suppress(AttributeError, KeyError):
            if isinstance(ex.args, tuple) and len(ex.args) > 1:
                message = ex.args[1]
        return message

    @classmethod
    def get_cancel_query_id(cls, cursor: Any, query: Query) -> str | None:
        cursor.execute("SELECT CONNECTION_ID()")
        row = cursor.fetchone()
        return row[0]

    @classmethod
    def cancel_query(cls, cursor: Any, query: Query, cancel_query_id: str) -> bool:
        try:
            cursor.execute(f"KILL CONNECTION {cancel_query_id}")
        except Exception:  # noqa: BLE001
            return False

        return True


__all__ = [
    "MySQLEngineSpec",
]
