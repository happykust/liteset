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
"""Column and metric name extraction helpers."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any, cast

# Re-export: the single canonical implementation lives in utils/core.py.
from superset.utils.core import get_time_filter_status  # noqa: F401

logger = logging.getLogger(__name__)

Column = str | dict[str, Any]
Metric = str | dict[str, Any]


def is_adhoc_metric(metric: Metric) -> bool:
    return isinstance(metric, dict) and (
        "expressionType" in metric or "expression_type" in metric
    )


def is_adhoc_column(column: Column) -> bool:
    return isinstance(column, dict) and (
        {"label", "sqlExpression"}.issubset(column.keys())
        or {"label", "sql_expression"}.issubset(column.keys())
    )


def is_base_axis(column: Column) -> bool:
    if not isinstance(column, dict):
        return False
    return is_adhoc_column(column) and (
        column.get("columnType") == "BASE_AXIS"
        or column.get("column_type") == "BASE_AXIS"
    )


def get_column_name(column: Column, verbose_map: dict[str, Any] | None = None) -> str:
    """
    Extract label from column.

    :param column: object to extract label from
    :param verbose_map: verbose_map from dataset for optional mapping from
                        raw name to verbose name
    :return: String representation of column
    :raises ValueError: if column object is invalid
    """
    if isinstance(column, dict):
        if label := column.get("label"):
            return label
        if expr := (column.get("sqlExpression") or column.get("sql_expression")):
            return expr

    if isinstance(column, str):
        verbose_map = verbose_map or {}
        return verbose_map.get(column, column)

    raise ValueError("Missing label")


def get_column_name_from_column(column: Column) -> str | None:
    """
    Extract the physical column that a column is referencing. If the column is
    an adhoc column, always returns ``None``.

    :param column: Physical and ad-hoc column
    :return: column name if physical column, otherwise None
    """
    if is_adhoc_column(column):
        return None
    return column  # type: ignore


def get_column_name_from_metric(metric: Metric) -> str | None:
    """
    Extract the column that a metric is referencing. If the metric isn't
    a simple metric, always returns ``None``.

    :param metric: Ad-hoc metric
    :return: column name if simple metric, otherwise None
    """
    if is_adhoc_metric(metric) and isinstance(metric, dict):
        expr_type = metric.get("expressionType") or metric.get("expression_type")
        if expr_type == "SIMPLE":
            col = metric.get("column")
            if isinstance(col, dict):
                return col.get("column_name")
    return None


def get_metric_name(metric: Metric, verbose_map: dict[str, Any] | None = None) -> str:
    """
    Extract label from metric.

    :param metric: object to extract label from
    :param verbose_map: verbose_map from dataset for optional mapping from
                        raw name to verbose name
    :return: String representation of metric
    :raises ValueError: if metric object is invalid
    """
    if is_adhoc_metric(metric) and isinstance(metric, dict):
        if label := metric.get("label"):
            return label
        expr_type = metric.get("expressionType") or metric.get("expression_type")
        sql_expr = metric.get("sqlExpression") or metric.get("sql_expression")
        if expr_type == "SQL":
            if sql_expr:
                return sql_expr
        if expr_type == "SIMPLE":
            column: dict[str, Any] = metric.get("column") or {}
            column_name = column.get("column_name")
            aggregate = metric.get("aggregate")
            if column and aggregate:
                return f"{aggregate}({column_name})"
            if column_name:
                return column_name

    if isinstance(metric, str):
        verbose_map = verbose_map or {}
        return verbose_map.get(metric, metric)

    raise ValueError(f"Invalid metric object: {metric}")


def get_column_names_from_columns(columns: list[Column]) -> list[str]:
    """
    Extract the physical columns that a list of columns are referencing.
    Ignore adhoc columns.

    :param columns: Physical and adhoc columns
    :return: column names of all physical columns
    """
    return [col for col in map(get_column_name_from_column, columns) if col]


def get_column_names_from_metrics(metrics: list[Metric]) -> list[str]:
    """
    Extract the columns that a list of metrics are referencing. Excludes all
    SQL metrics.

    :param metrics: Ad-hoc metrics
    :return: column names from simple metrics
    """
    return [col for col in map(get_column_name_from_metric, metrics) if col]


def get_column_names(
    columns: Sequence[Column] | None,
    verbose_map: dict[str, Any] | None = None,
) -> list[str]:
    return [
        column
        for column in [get_column_name(column, verbose_map) for column in columns or []]
        if column
    ]


def get_metric_names(
    metrics: Sequence[Metric] | None,
    verbose_map: dict[str, Any] | None = None,
) -> list[str]:
    return [
        metric
        for metric in [get_metric_name(metric, verbose_map) for metric in metrics or []]
        if metric
    ]


def get_non_base_axis_columns(columns: list[Column] | None) -> list[Column]:
    return [column for column in columns or [] if not is_base_axis(column)]


def get_base_axis_columns(columns: list[Column] | None) -> list[Column]:
    return [column for column in columns or [] if is_base_axis(column)]


def get_base_axis_labels(columns: list[Column] | None) -> tuple[str, ...]:
    return tuple(get_column_name(column) for column in get_base_axis_columns(columns))


def get_x_axis_label(columns: list[Column] | None) -> str | None:
    labels = get_base_axis_labels(columns)
    return labels[0] if labels else None


_TYPE_MAPPING = {
    re.compile(r"INT", re.IGNORECASE): "integer",
    re.compile(r"CHAR|TEXT|VARCHAR", re.IGNORECASE): "string",
    re.compile(r"DECIMAL|NUMERIC|FLOAT|DOUBLE", re.IGNORECASE): "floating",
    re.compile(r"BOOL", re.IGNORECASE): "boolean",
    re.compile(r"DATE|TIME", re.IGNORECASE): "datetime64",
}

_METRIC_MAP_TYPE = {
    "SUM": "floating",
    "AVG": "floating",
    "COUNT": "floating",
    "COUNT_DISTINCT": "floating",
    "MIN": "numeric",
    "MAX": "numeric",
    "FIRST": "string",
    "LAST": "string",
    "GROUP_CONCAT": "string",
    "ARRAY_AGG": "string",
    "STRING_AGG": "string",
    "MEDIAN": "floating",
    "PERCENTILE": "floating",
    "VARIANCE": "floating",
    "STDDEV": "floating",
}


def _map_sql_type_to_inferred_type(sql_type: str | None) -> str:
    if not sql_type:
        return "string"

    for pattern, inferred_type in _TYPE_MAPPING.items():
        if pattern.search(sql_type):
            return inferred_type

    return "string"


def _get_metric_type_from_column(column: Any, datasource: Any) -> str:
    from superset.models.connectors import SqlMetric

    metric: SqlMetric = next(
        (m for m in datasource.metrics if m.metric_name == column),
        SqlMetric(metric_name=""),
    )

    if metric.metric_name == "":
        return ""

    expression: str = cast("str", metric.expression)

    match = re.match(
        r"(SUM|AVG|COUNT|COUNT_DISTINCT|MIN|MAX|FIRST|LAST)\((.*)\)", expression
    )

    if match:
        operation = match.group(1)
        return _METRIC_MAP_TYPE.get(operation, "")

    logger.warning("Unexpected metric expression type: %s", expression)
    return ""


def extract_dataframe_dtypes(
    df: Any,
    datasource: Any | None = None,
) -> list[int]:
    """Serialize pandas/numpy dtypes to generic types.

    Handles:
    1. Building ``columns_by_name`` from ``datasource.columns`` (both dict
       and ORM objects).
    2. All-NaN columns: falls back to ``datasource.columns_types`` +
       ``map_sql_type_to_inferred_type``, or ``get_metric_type_from_column``
       (using ``METRIC_MAP_TYPE`` dict with SQL aggregation regex matching).
    3. Non-NaN columns: uses ``pandas.api.types.infer_dtype()`` for
       content-based type detection, mapped through ``inferred_type_map``.
    4. ``is_dttm`` override on datasource column objects.

    :param df: pandas DataFrame
    :param datasource: optional datasource with column metadata
    :return: list of GenericDataType int values for each column
    """
    from pandas.api.types import infer_dtype

    from superset.typing import GenericDataType

    # omitting string types as those will be the default type
    inferred_type_map: dict[str, GenericDataType] = {
        "floating": GenericDataType.NUMERIC,
        "integer": GenericDataType.NUMERIC,
        "mixed-integer-float": GenericDataType.NUMERIC,
        "decimal": GenericDataType.NUMERIC,
        "boolean": GenericDataType.BOOLEAN,
        "datetime64": GenericDataType.TEMPORAL,
        "datetime": GenericDataType.TEMPORAL,
        "date": GenericDataType.TEMPORAL,
    }

    columns_by_name: dict[str, Any] = {}
    if datasource:
        for column in datasource.columns:
            if isinstance(column, dict):
                columns_by_name[column.get("column_name")] = column  # type: ignore[index]
            else:
                columns_by_name[column.column_name] = column

    generic_types: list[int] = []
    for column in df.columns:
        column_object = columns_by_name.get(column)
        series = df[column]
        inferred_type: str = ""
        if series.isna().all():
            sql_type: str | None = ""
            if datasource and hasattr(datasource, "columns_types"):
                if column in datasource.columns_types:
                    sql_type = datasource.columns_types.get(column)
                    inferred_type = _map_sql_type_to_inferred_type(sql_type)
                else:
                    inferred_type = _get_metric_type_from_column(column, datasource)
        else:
            inferred_type = infer_dtype(series)
        if isinstance(column_object, dict):
            generic_type = (
                GenericDataType.TEMPORAL
                if column_object and column_object.get("is_dttm")
                else inferred_type_map.get(inferred_type, GenericDataType.STRING)
            )
        else:
            generic_type = (
                GenericDataType.TEMPORAL
                if column_object and column_object.is_dttm
                else inferred_type_map.get(inferred_type, GenericDataType.STRING)
            )
        generic_types.append(generic_type)

    return generic_types


T = Any  # TypeVar not needed for the simple key-based dedup


def remove_duplicates(
    lst: list[Any],
    key: Any | None = None,
) -> list[Any]:
    """Remove duplicates from a list, preserving order.

    :param lst: list of items
    :param key: optional callable that returns a dedup key for each item
    :return: deduplicated list
    """
    seen: set[Any] = set()
    result: list[Any] = []
    for item in lst:
        k = key(item) if key else item
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


def cast_to_num(value: float | int | str | None) -> float | int | None:
    """Casts a value to an int/float

    >>> cast_to_num('1 ')
    1.0
    >>> cast_to_num(' 2')
    2.0
    >>> cast_to_num('5')
    5
    >>> cast_to_num('5.2')
    5.2
    >>> cast_to_num(10)
    10
    >>> cast_to_num(10.1)
    10.1
    >>> cast_to_num(None) is None
    True
    >>> cast_to_num('this is not a string') is None
    True

    :param value: value to be converted to numeric representation
    :returns: value cast to `int` if value is all digits, `float` if `value` is
              decimal value and `None`` if it can't be converted
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if value.isdigit():
        return int(value)
    try:
        return float(value)
    except ValueError:
        return None


def cast_to_boolean(value: Any) -> bool | None:
    """Casts a value to an int/float

    >>> cast_to_boolean(1)
    True
    >>> cast_to_boolean(0)
    False
    >>> cast_to_boolean(0.5)
    True
    >>> cast_to_boolean('true')
    True
    >>> cast_to_boolean('false')
    False
    >>> cast_to_boolean('False')
    False
    >>> cast_to_boolean(None)

    :param value: value to be converted to boolean representation
    :returns: value cast to `bool`. when value is 'true' or value that are not 0
              converted into True. Return `None` if value is `None`
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def error_msg_from_exception(ex: Exception) -> str:
    """Translate an exception into a human-readable error message.

    Database drivers expose error info in different ways – this function
    inspects ``ex.message`` (which may be a dict for Presto and similar
    drivers) and falls back to ``str(ex)`` when nothing more specific is
    available.

    :param ex: exception
    :return: error message string
    """
    msg: Any = ""
    if hasattr(ex, "message"):
        if isinstance(ex.message, dict):
            msg = ex.message.get("message")
        elif ex.message:
            msg = ex.message
    return str(msg) or str(ex)
