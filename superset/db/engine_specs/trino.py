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
"""Native async engine spec for Trino using aiotrino driver."""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from datetime import datetime
from enum import IntEnum
from typing import Any, cast

from sqlalchemy import types
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text

from superset.db.engine_specs.base import (
    AsyncResultSet,
    BaseAsyncEngineSpec,
    ColumnTypeMapping,
)
from superset.typing import GenericDataType

logger = logging.getLogger(__name__)


class _GenericDataType(IntEnum):
    NUMERIC = 0
    STRING = 1
    TEMPORAL = 2
    BOOLEAN = 3


def _split_complex(s: str, delimiter: str = ",") -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == delimiter and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


def get_children(
    column: dict[str, Any],
) -> list[dict[str, Any]]:
    """Get children of a complex Presto/Trino type (ROW or ARRAY).

    For ARRAYs we return a single element with the base type.
    For ROWs we return one element per inner field.
    """
    pattern = re.compile(r"(?P<type>\w+)\((?P<children>.*)\)")
    col_type = column.get("type") or column.get("column_name", "")
    if not col_type:
        raise ValueError("Column type is empty")
    match = pattern.match(str(col_type))
    if not match:
        raise ValueError(f"Unable to parse column type {col_type}")

    group = match.groupdict()
    type_name = group["type"].upper()
    children_type = group["children"]

    if type_name == "ARRAY":
        return [
            {
                "column_name": column["column_name"],
                "name": column["column_name"],
                "type": children_type,
                "is_dttm": False,
            }
        ]

    if type_name == "ROW":
        nameless_columns = 0
        columns: list[dict[str, Any]] = []
        for child in _split_complex(children_type, ","):
            parts = _split_complex(child.strip(), " ")
            if len(parts) == 2:
                name, inner_type = parts
                name = name.strip('"')
            else:
                name = f"_col{nameless_columns}"
                inner_type = parts[0]
                nameless_columns += 1
            columns.append(
                {
                    "column_name": f"{column['column_name']}.{name.lower()}",
                    "name": f"{column['column_name']}.{name.lower()}",
                    "type": inner_type,
                    "is_dttm": False,
                }
            )
        return columns

    raise ValueError(f"Unknown complex type {type_name}")


def _destringify(value: str) -> Any:
    import json as _json

    try:
        return _json.loads(value)
    except (_json.JSONDecodeError, TypeError):
        return value


class AsyncTrinoEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for Trino using aiotrino driver."""

    engine = "trino"
    engine_name = "Trino"
    default_driver = "aiotrino"

    supports_dynamic_schema: bool = True
    supports_catalog: bool = True

    column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r"^boolean.*", re.IGNORECASE),
            types.BOOLEAN(),
            GenericDataType.BOOLEAN,
        ),
        (
            re.compile(r"^tinyint.*", re.IGNORECASE),
            types.SmallInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^smallint.*", re.IGNORECASE),
            types.SmallInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^integer.*", re.IGNORECASE),
            types.INTEGER(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bigint.*", re.IGNORECASE),
            types.BigInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^real.*", re.IGNORECASE),
            types.FLOAT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^double.*", re.IGNORECASE),
            types.FLOAT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^decimal.*", re.IGNORECASE),
            types.DECIMAL(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^varchar(\((\d+)\))*$", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^char(\((\d+)\))*$", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^varbinary.*", re.IGNORECASE),
            types.VARBINARY(),
            GenericDataType.STRING,
        ),
        (re.compile(r"^json.*", re.IGNORECASE), types.JSON(), GenericDataType.STRING),
        (
            re.compile(r"^date.*", re.IGNORECASE),
            types.Date(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^timestamp.*", re.IGNORECASE),
            types.TIMESTAMP(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^interval.*", re.IGNORECASE),
            types.String(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^time.*", re.IGNORECASE),
            types.Time(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^array.*", re.IGNORECASE),
            types.String(),
            GenericDataType.STRING,
        ),
        (re.compile(r"^map.*", re.IGNORECASE), types.String(), GenericDataType.STRING),
        (re.compile(r"^row.*", re.IGNORECASE), types.String(), GenericDataType.STRING),
    )

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": "DATE_TRUNC('second', CAST({col} AS TIMESTAMP))",
        "PT5S": (
            "DATE_TRUNC('second', CAST({col} AS TIMESTAMP))"
            " - interval '1' second * (second(CAST({col} AS TIMESTAMP)) % 5)"
        ),
        "PT30S": (
            "DATE_TRUNC('second', CAST({col} AS TIMESTAMP))"
            " - interval '1' second * (second(CAST({col} AS TIMESTAMP)) % 30)"
        ),
        "PT1M": "DATE_TRUNC('minute', CAST({col} AS TIMESTAMP))",
        "PT5M": (
            "DATE_TRUNC('minute', CAST({col} AS TIMESTAMP))"
            " - interval '1' minute * (minute(CAST({col} AS TIMESTAMP)) % 5)"
        ),
        "PT10M": (
            "DATE_TRUNC('minute', CAST({col} AS TIMESTAMP))"
            " - interval '1' minute * (minute(CAST({col} AS TIMESTAMP)) % 10)"
        ),
        "PT15M": (
            "DATE_TRUNC('minute', CAST({col} AS TIMESTAMP))"
            " - interval '1' minute * (minute(CAST({col} AS TIMESTAMP)) % 15)"
        ),
        # Half hour — upstream presto/trino key is TimeGrain.HALF_HOUR
        # ("PT0.5H"), NOT "PT30M". The explore UI offers grains from the SYNC
        # spec table, so any async consumer of this table must see the same
        # keys or it silently drops the truncation (R13-09, latent — see
        # tests/superset/unit/test_engine_spec_grain_parity.py).
        "PT0.5H": (
            "DATE_TRUNC('minute', CAST({col} AS TIMESTAMP))"
            " - interval '1' minute * (minute(CAST({col} AS TIMESTAMP)) % 30)"
        ),
        "PT1H": "DATE_TRUNC('hour', CAST({col} AS TIMESTAMP))",
        "PT6H": (
            "DATE_TRUNC('hour', CAST({col} AS TIMESTAMP))"
            " - interval '1' hour * (hour(CAST({col} AS TIMESTAMP)) % 6)"
        ),
        "P1D": "DATE_TRUNC('day', CAST({col} AS TIMESTAMP))",
        "P1W": "DATE_TRUNC('week', CAST({col} AS TIMESTAMP))",
        "P1M": "DATE_TRUNC('month', CAST({col} AS TIMESTAMP))",
        "P3M": "DATE_TRUNC('quarter', CAST({col} AS TIMESTAMP))",
        "P1Y": "DATE_TRUNC('year', CAST({col} AS TIMESTAMP))",
        "1969-12-28T00:00:00Z/P1W": (
            "DATE_TRUNC('week', CAST({col} AS TIMESTAMP) + interval '1' day)"
            " - interval '1' day"
        ),
        "1969-12-29T00:00:00Z/P1W": "DATE_TRUNC('week', CAST({col} AS TIMESTAMP))",
        "P1W/1970-01-03T00:00:00Z": (
            "DATE_TRUNC('week', CAST({col} AS TIMESTAMP) + interval '1' day)"
            " + interval '5' day"
        ),
        "P1W/1970-01-04T00:00:00Z": (
            "DATE_TRUNC('week', CAST({col} AS TIMESTAMP)) + interval '6' day"
        ),
    }

    _custom_errors: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(
                r"line (?P<line>\d+):(?P<col>\d+): "
                r"Column '(?P<column>.+?)' cannot be resolved"
            ),
            "Column '{column}' cannot be resolved (line {line}:{col})",
        ),
        (
            re.compile(r"Table '(?P<table>.+?)' does not exist"),
            "Table '{table}' does not exist",
        ),
        (
            re.compile(r"Schema '(?P<schema>.+?)' does not exist"),
            "Schema '{schema}' does not exist",
        ),
        (
            re.compile(r"Catalog '(?P<catalog>.+?)' does not exist"),
            "Catalog '{catalog}' does not exist",
        ),
        (
            re.compile(r"Access Denied"),
            "Access denied",
        ),
    ]

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "from_unixtime({col})"

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        tt = target_type.upper().strip()
        if tt == "DATE":
            return f"DATE '{dttm.date().isoformat()}'"
        if tt.startswith("TIMESTAMP"):
            return f"""TIMESTAMP '{dttm.isoformat(timespec="microseconds", sep=" ")}'"""
        return None

    @classmethod
    def get_allow_cost_estimate(cls, extra: dict[str, Any] | None = None) -> bool:
        return True

    @classmethod
    async def estimate_statement_cost(
        cls,
        conn: AsyncConnection,
        statement: str,
    ) -> dict[str, Any]:
        import json as _json

        sql = f"EXPLAIN (TYPE IO, FORMAT JSON) {statement}"
        result = await conn.execute(text(sql))
        row = result.fetchone()
        return _json.loads(row[0]) if row else {}

    @classmethod
    def query_cost_formatter(
        cls,
        raw_cost: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        def humanize(value: Any, suffix: str) -> str:
            try:
                value = int(value)
            except (ValueError, TypeError):
                return str(value)
            prefixes = ["K", "M", "G", "T", "P", "E", "Z", "Y"]
            prefix = ""
            to_next_prefix = 1000
            while value > to_next_prefix and prefixes:
                prefix = prefixes.pop(0)
                value //= to_next_prefix
            return f"{value} {prefix}{suffix}"

        cost: list[dict[str, str]] = []
        columns = [
            ("outputRowCount", "Output count", " rows"),
            ("outputSizeInBytes", "Output size", "B"),
            ("cpuCost", "CPU cost", ""),
            ("maxMemory", "Max memory", "B"),
            ("networkCost", "Network cost", ""),
        ]
        for row in raw_cost:
            estimate: dict[str, float] = row.get("estimate", {})
            statement_cost: dict[str, str] = {}
            for key, label, suffix in columns:
                if key in estimate:
                    statement_cost[label] = humanize(estimate[key], suffix).strip()
            cost.append(statement_cost)
        return cost

    @classmethod
    async def cancel_query(
        cls,
        conn: AsyncConnection,
        cancel_query_id: str,
    ) -> bool:
        try:
            await conn.execute(
                text(
                    "CALL system.runtime.kill_query("
                    "query_id => :query_id, "
                    "message => 'Query cancelled by Superset')"
                ),
                {"query_id": cancel_query_id},
            )
            return True
        except Exception:
            logger.exception("Failed to cancel Trino query %s", cancel_query_id)
            return False

    @classmethod
    async def get_function_names(
        cls,
        conn: AsyncConnection,
    ) -> list[str]:
        result = await conn.execute(text("SHOW FUNCTIONS"))
        return [row[0] for row in result.fetchall()]

    @classmethod
    async def get_extra_table_metadata(
        cls,
        conn: AsyncConnection,
        table_name: str,
        schema: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {}

        system_table = f'"{table_name}$partitions"'
        full_table = f"{schema}.{system_table}" if schema else system_table
        try:
            result = await conn.execute(text(f"SELECT * FROM {full_table} LIMIT 1"))  # noqa: S608
            columns = list(result.keys())
            rows = result.fetchall()
            if columns:
                latest_parts = dict(zip(columns, rows[0], strict=False)) if rows else {}
                metadata["partitions"] = {
                    "cols": sorted(columns),
                    "latest": latest_parts,
                    "partitionQuery": f"SELECT * FROM {full_table}",  # noqa: S608
                }
        except Exception:
            logger.debug(
                "No partition info for %s.%s", schema, table_name, exc_info=True
            )

        try:
            result = await conn.execute(
                text(
                    "SELECT view_definition FROM information_schema.views "  # noqa: S608
                    "WHERE table_name = :table_name"
                    + (" AND table_schema = :schema" if schema else "")
                ),
                {"table_name": table_name, **({"schema": schema} if schema else {})},
            )
            row = result.fetchone()
            if row:
                metadata["view"] = row[0]
        except Exception:  # noqa: S110
            pass

        return metadata

    @classmethod
    async def get_columns(
        cls,
        conn: AsyncConnection,
        table_name: str,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return column metadata, falling back to ``SHOW COLUMNS`` on error.

        The Trino dialect can raise errors when querying ``information_schema.columns``
        for empty tables. We fall back to ``SHOW COLUMNS FROM`` in that case.
        """
        try:
            return await super().get_columns(conn, table_name, schema)
        except NoSuchTableError:
            logger.debug(
                "information_schema.columns failed for %s.%s,"
                " falling back to SHOW COLUMNS",
                schema,
                table_name,
                exc_info=True,
            )
            qualified = f"{schema}.{table_name}" if schema else table_name
            result = await conn.execute(text(f"SHOW COLUMNS FROM {qualified}"))
            return [
                {
                    "column_name": row[0],
                    "data_type": row[1] if len(row) > 1 else "VARCHAR",
                    "is_nullable": True,
                }
                for row in result.fetchall()
            ]

    @classmethod
    def expand_data(  # noqa: C901
        cls,
        columns: list[dict[str, Any]],
        data: list[dict[Any, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[Any, Any]], list[dict[str, Any]]]:
        """Unnest ARRAY types and expand ROW types into dotted columns.

        Returns ``(all_columns, expanded_data, expanded_columns)``.
        """
        to_process: deque[tuple[dict[str, Any], int]] = deque(
            (column, 0) for column in columns
        )
        all_columns: list[dict[str, Any]] = []
        expanded_columns: list[dict[str, Any]] = []
        current_array_level: int | None = None
        unnested_rows: dict[int, int] = defaultdict(int)

        while to_process:
            column, level = to_process.popleft()
            if column["column_name"] not in [c["column_name"] for c in all_columns]:
                all_columns.append(column)

            if level != current_array_level:
                unnested_rows = defaultdict(int)
                current_array_level = level

            name = column["column_name"]
            col_type = cast(str, column.get("type") or "")

            if col_type.upper().startswith("ARRAY("):
                to_process.append((get_children(column)[0], level + 1))

                i = 0
                while i < len(data):
                    row = data[i]
                    values = row.get(name)
                    if isinstance(values, str):
                        row[name] = values = _destringify(values)
                    if isinstance(values, (list, tuple)) and values:
                        extra_rows = len(values) - 1
                        current_unnested = unnested_rows[i]
                        missing = extra_rows - current_unnested
                        for _ in range(missing):
                            data.insert(i + current_unnested + 1, {})
                            unnested_rows[i] += 1
                        for j, value in enumerate(values):
                            data[i + j][name] = value
                        i += unnested_rows[i]
                    i += 1

            if col_type.upper().startswith("ROW("):
                expanded = get_children(column)
                to_process.extendleft((child, level) for child in expanded[::-1])
                expanded_columns.extend(expanded)

                for row in data:
                    values = row.get(name) or []
                    if isinstance(values, str):
                        values = _destringify(values)
                        row[name] = values
                    if isinstance(values, (list, tuple)):
                        for value, col in zip(values, expanded, strict=False):
                            row[col["column_name"]] = value

        data = [
            {k["column_name"]: row.get(k["column_name"], "") for k in all_columns}
            for row in data
        ]

        return all_columns, data, expanded_columns

    @classmethod
    async def execute(
        cls,
        conn: AsyncConnection,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> AsyncResultSet:
        return await cls._default_execute(conn, query, parameters)

    @classmethod
    async def fetch_data(
        cls,
        conn: AsyncConnection,
        query: str,
        limit: int | None = None,
    ) -> list[tuple[Any, ...]]:
        return await cls._default_fetch_data(conn, query, limit)
