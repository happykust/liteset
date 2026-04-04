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
"""ClickHouse engine spec -- sync/Flask-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/clickhouse.py`` with Flask
imports removed.  Only overridden methods and attributes are included.

Includes:
  - ``ClickHouseBaseEngineSpec`` -- shared base for all ClickHouse connectors
  - ``ClickHouseEngineSpec``     -- clickhouse_sqlalchemy connector
  - ``ClickHouseConnectEngineSpec`` -- clickhouse-connect connector
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, TYPE_CHECKING
from urllib import parse

from sqlalchemy import types
from sqlalchemy.engine.url import URL

from superset.db_engine_specs.base import BaseEngineSpec, ColumnTypeMapping
from superset.typing import GenericDataType
from superset.utils.hashing import md5_sha_from_str

if TYPE_CHECKING:
    from superset.models.core import Database

logger = logging.getLogger(__name__)


class ClickHouseBaseEngineSpec(BaseEngineSpec):
    """Shared engine spec for ClickHouse."""

    time_groupby_inline = True

    _time_grain_expressions = {
        None: "{col}",
        "PT1M": "toStartOfMinute(toDateTime({col}))",
        "PT5M": "toDateTime(intDiv(toUInt32(toDateTime({col})), 300)*300)",
        "PT10M": "toDateTime(intDiv(toUInt32(toDateTime({col})), 600)*600)",
        "PT15M": "toDateTime(intDiv(toUInt32(toDateTime({col})), 900)*900)",
        "PT30M": "toDateTime(intDiv(toUInt32(toDateTime({col})), 1800)*1800)",
        "PT1H": "toStartOfHour(toDateTime({col}))",
        "P1D": "toStartOfDay(toDateTime({col}))",
        "P1W": "toMonday(toDateTime({col}))",
        "P1M": "toStartOfMonth(toDateTime({col}))",
        "P3M": "toStartOfQuarter(toDateTime({col}))",
        "P1Y": "toStartOfYear(toDateTime({col}))",
    }

    column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r".*Enum.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*Array.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*UUID.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*Bool.*", re.IGNORECASE),
            types.Boolean(),
            GenericDataType.BOOLEAN,
        ),
        (
            re.compile(r".*String.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r".*Int\d+.*", re.IGNORECASE),
            types.INTEGER(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r".*Decimal.*", re.IGNORECASE),
            types.DECIMAL(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r".*DateTime.*", re.IGNORECASE),
            types.DateTime(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r".*Date.*", re.IGNORECASE),
            types.Date(),
            GenericDataType.TEMPORAL,
        ),
    )

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "{col}"

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"toDate('{dttm.date().isoformat()}')"
        if isinstance(sqla_type, types.DateTime):
            return f"""toDateTime('{dttm.isoformat(sep=" ", timespec="seconds")}')"""
        return None


class ClickHouseEngineSpec(ClickHouseBaseEngineSpec):
    """Engine spec for clickhouse_sqlalchemy connector."""

    engine = "clickhouse"
    engine_name = "ClickHouse"

    _show_functions_column = "name"
    supports_file_upload = False

    @classmethod
    def get_function_names(cls, database: Database) -> list[str]:
        """Get a list of function names from system.functions.

        :param database: The database to get functions for
        :return: A list of function names usable in the database
        """
        system_functions_sql = "SELECT name FROM system.functions"
        try:
            df = database.get_df(system_functions_sql)
            if cls._show_functions_column in df:
                return df[cls._show_functions_column].tolist()
            columns = df.columns.values.tolist()
            logger.error(
                "Payload from `%s` has the incorrect format. "
                "Expected column `%s`, found: %s.",
                system_functions_sql,
                cls._show_functions_column,
                ", ".join(columns),
                exc_info=True,
            )
            # if the results have a single column, use that
            if len(columns) == 1:
                return df[columns[0]].tolist()
        except Exception:  # noqa: BLE001
            logger.error(
                "Query `%s` fired error.",
                system_functions_sql,
                exc_info=True,
            )
            return []

        # otherwise, return no function names to prevent errors
        return []


class ClickHouseConnectEngineSpec(ClickHouseEngineSpec):
    """Engine spec for clickhouse-connect connector."""

    engine = "clickhousedb"
    engine_name = "ClickHouse Connect (Superset)"

    default_driver = "connect"
    _function_names: list[str] = []

    sqlalchemy_uri_placeholder = (
        "clickhousedb://user:password@host[:port][/dbname][?secure=value&=value...]"
    )
    encryption_parameters = {"secure": "true"}

    supports_dynamic_schema = True

    @classmethod
    def get_function_names(cls, database: Database) -> list[str]:
        if cls._function_names:
            return cls._function_names
        try:
            names = database.get_df(
                "SELECT name FROM system.functions UNION ALL "  # noqa: S608
                + "SELECT name FROM system.table_functions LIMIT 10000"
            )["name"].tolist()
            cls._function_names = names
            return names
        except Exception:  # noqa: BLE001
            logger.exception("Error retrieving system.functions")
            return []

    @classmethod
    def get_datatype(cls, type_code: str) -> str:
        # keep it lowercase, as ClickHouse types aren't typical SHOUTCASE ANSI SQL
        return type_code

    @staticmethod
    def _mutate_label(label: str) -> str:
        """Suffix with the first six characters from the md5 of the label
        to avoid collisions with original column names.

        :param label: Expected expression label
        :return: Conditionally mutated label
        """
        return f"{label}_{md5_sha_from_str(label)[:6]}"

    @classmethod
    def adjust_engine_params(
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        if schema:
            uri = uri.set(database=parse.quote(schema, safe=""))
        return uri, connect_args


__all__ = [
    "ClickHouseBaseEngineSpec",
    "ClickHouseEngineSpec",
    "ClickHouseConnectEngineSpec",
]
