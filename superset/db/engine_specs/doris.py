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

from sqlalchemy import Float, Integer, Numeric, String, TEXT, types
from sqlalchemy.sql.type_api import TypeEngine

from superset.db.engine_specs.base import ColumnTypeMapping
from superset.db.engine_specs.mysql import AsyncMySQLEngineSpec
from superset.typing import GenericDataType


class TINYINT(Integer):
    __visit_name__ = "TINYINT"


class LARGEINT(Integer):
    __visit_name__ = "LARGEINT"


class DOUBLE(Float[Any]):
    __visit_name__ = "DOUBLE"


class HLL(Numeric[Any]):
    __visit_name__ = "HLL"


class BITMAP(Numeric[Any]):
    __visit_name__ = "BITMAP"


class QuantileState(Numeric[Any]):
    __visit_name__ = "QUANTILE_STATE"


class AggState(Numeric[Any]):
    __visit_name__ = "AGG_STATE"


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


class STRUCT(TypeEngine[Any]):
    __visit_name__ = "STRUCT"

    @property
    def python_type(self) -> type[Any]:
        return object


class AsyncDorisEngineSpec(AsyncMySQLEngineSpec):
    """Async engine spec for Apache Doris (MySQL wire protocol)."""

    engine = "doris"
    engine_name = "Apache Doris"
    default_driver = "aiomysql"

    supports_dynamic_schema: bool = True
    supports_catalog: bool = True

    column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (re.compile(r"^tinyint", re.IGNORECASE), TINYINT(), GenericDataType.NUMERIC),
        (re.compile(r"^largeint", re.IGNORECASE), LARGEINT(), GenericDataType.NUMERIC),
        (
            re.compile(r"^decimal.*", re.IGNORECASE),
            types.DECIMAL(),
            GenericDataType.NUMERIC,
        ),
        (re.compile(r"^double", re.IGNORECASE), DOUBLE(), GenericDataType.NUMERIC),
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
        (re.compile(r"^bitmap", re.IGNORECASE), BITMAP(), GenericDataType.STRING),
        (re.compile(r"^array.*", re.IGNORECASE), ARRAY(), GenericDataType.STRING),
        (re.compile(r"^map.*", re.IGNORECASE), MAP(), GenericDataType.STRING),
        (re.compile(r"^struct.*", re.IGNORECASE), STRUCT(), GenericDataType.STRING),
        (
            re.compile(r"^datetime.*", re.IGNORECASE),
            types.DATETIME(),
            GenericDataType.TEMPORAL,
        ),
        (re.compile(r"^date.*", re.IGNORECASE), types.DATE(), GenericDataType.TEMPORAL),
        (re.compile(r"^text.*", re.IGNORECASE), TEXT(), GenericDataType.STRING),
        (re.compile(r"^string.*", re.IGNORECASE), String(), GenericDataType.STRING),
    )

    _custom_errors: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r"Access denied for user '(?P<username>.*?)'"),
            "Access denied for user: {username}",
        ),
        (
            re.compile(r"Unknown Doris server host '(?P<host>.*?)'"),
            "Unknown hostname: {host}",
        ),
        (
            re.compile(r"Can't connect to Doris server on '(?P<host>.*?)'"),
            "Cannot connect to Doris server: {host}",
        ),
        (
            re.compile(r"Unknown database '(?P<database>.*?)'"),
            "Unknown database: {database}",
        ),
    ]
