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

from collections.abc import Sequence
from typing import Any

# Type aliases matching superset's typing
Column = str | dict[str, Any]
Metric = str | dict[str, Any]


def is_adhoc_metric(metric: Metric) -> bool:
    """Check if metric is an adhoc metric (dict with expressionType)."""
    return isinstance(metric, dict) and "expressionType" in metric


def is_adhoc_column(column: Column) -> bool:
    """Check if column is an adhoc column (dict with label and sqlExpression)."""
    return isinstance(column, dict) and {"label", "sqlExpression"}.issubset(
        column.keys()
    )


def is_base_axis(column: Column) -> bool:
    """Check if column is a base axis column."""
    return is_adhoc_column(column) and column.get("columnType") == "BASE_AXIS"


def get_column_name(
    column: Column, verbose_map: dict[str, Any] | None = None
) -> str:
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
        if expr := column.get("sqlExpression"):
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
    if is_adhoc_metric(metric):
        if metric.get("expressionType") == "SIMPLE":
            col = metric.get("column")
            if isinstance(col, dict):
                return col.get("column_name")
    return None


def get_metric_name(
    metric: Metric, verbose_map: dict[str, Any] | None = None
) -> str:
    """
    Extract label from metric.

    :param metric: object to extract label from
    :param verbose_map: verbose_map from dataset for optional mapping from
                        raw name to verbose name
    :return: String representation of metric
    :raises ValueError: if metric object is invalid
    """
    if is_adhoc_metric(metric):
        if label := metric.get("label"):
            return label
        if metric.get("expressionType") == "SQL":
            if sql_expression := metric.get("sqlExpression"):
                return sql_expression
        if metric.get("expressionType") == "SIMPLE":
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
    """Extract column names from a list of columns."""
    return [
        column
        for column in [
            get_column_name(column, verbose_map) for column in columns or []
        ]
        if column
    ]


def get_metric_names(
    metrics: Sequence[Metric] | None,
    verbose_map: dict[str, Any] | None = None,
) -> list[str]:
    """Extract metric names from a list of metrics."""
    return [
        metric
        for metric in [
            get_metric_name(metric, verbose_map) for metric in metrics or []
        ]
        if metric
    ]


def get_base_axis_columns(columns: list[Column] | None) -> list[Column]:
    """Return columns marked as BASE_AXIS."""
    return [column for column in columns or [] if is_base_axis(column)]


def get_base_axis_labels(columns: list[Column] | None) -> tuple[str, ...]:
    """Return labels for base axis columns."""
    return tuple(
        get_column_name(column) for column in get_base_axis_columns(columns)
    )


def get_x_axis_label(columns: list[Column] | None) -> str | None:
    """Return the first base axis label, or None."""
    labels = get_base_axis_labels(columns)
    return labels[0] if labels else None
