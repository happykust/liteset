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
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Numeric, TEXT, types
from sqlalchemy.sql.type_api import TypeEngine

from superset.db.engine_specs.base import ColumnTypeMapping
from superset.db.engine_specs.mysql import AsyncMySQLEngineSpec
from superset.typing import GenericDataType


class NUMBER(Numeric[Any]):
    __visit_name__ = "NUMBER"


class ObNumeric(Numeric[Any]):
    __visit_name__ = "NUMERIC"


class ARRAY(TypeEngine[list[Any]]):
    __visit_name__ = "ARRAY"

    @property
    def python_type(self) -> type[list[Any]]:
        return list


class MAP(TypeEngine[dict[Any, Any]]):
    __visit_name__ = "MAP"

    @property
    def python_type(self) -> type[dict[Any, Any]]:
        return dict


class AsyncOceanBaseEngineSpec(AsyncMySQLEngineSpec):
    """Async engine spec for OceanBase (MySQL wire protocol)."""

    engine = "oceanbase"
    engine_name = "OceanBase"
    default_driver = "aiomysql"
    max_column_name_length = 128

    supports_dynamic_schema: bool = True

    column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r"^tinyint", re.IGNORECASE),
            types.SMALLINT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^largeint", re.IGNORECASE),
            types.BIGINT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^decimal.*", re.IGNORECASE),
            types.DECIMAL(),
            GenericDataType.NUMERIC,
        ),
        (re.compile(r"^double", re.IGNORECASE), types.FLOAT(), GenericDataType.NUMERIC),
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
        (re.compile(r"^json.*", re.IGNORECASE), types.JSON(), GenericDataType.STRING),
        (
            re.compile(r"^binary.*", re.IGNORECASE),
            types.BINARY(),
            GenericDataType.STRING,
        ),
        (re.compile(r"^array.*", re.IGNORECASE), ARRAY(), GenericDataType.STRING),
        (re.compile(r"^map.*", re.IGNORECASE), MAP(), GenericDataType.STRING),
        (re.compile(r"^text.*", re.IGNORECASE), TEXT(), GenericDataType.STRING),
        (re.compile(r"^number.*", re.IGNORECASE), NUMBER(), GenericDataType.NUMERIC),
        (
            re.compile(r"^numeric.*", re.IGNORECASE),
            ObNumeric(),
            GenericDataType.NUMERIC,
        ),
    )

    _custom_errors: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r"Access denied for user '(?P<username>.*?)'"),
            "Access denied for user: {username}",
        ),
        (
            re.compile(r"Unknown OceanBase server host '(?P<host>.*?)'"),
            "Unknown hostname: {host}",
        ),
        (
            re.compile(r"Can't connect to OceanBase server on '(?P<host>.*?)'"),
            "Cannot connect to OceanBase server: {host}",
        ),
        (
            re.compile(r"Unknown database '(?P<database>.*?)'"),
            "Unknown database: {database}",
        ),
    ]
