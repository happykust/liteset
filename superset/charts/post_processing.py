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
"""
Functions to reproduce the client post-processing of data on charts.

Some text-based charts (pivot tables and t-test table) perform post-processing of
the data in JavaScript. When sending the data to users in reports we want to
show the same data they would see on Explore.

In order to do that, we reproduce the post-processing in Python for these chart
types.

Helper utilities (``extract_dataframe_dtypes``, ``get_column_names``,
``get_metric_names``) live in :mod:`superset.utils.column`.
"""

from __future__ import annotations

import logging
from io import StringIO
from typing import Any, Callable, Optional, TYPE_CHECKING, Union

import numpy as np
import pandas as pd

from superset.utils.column import (
    extract_dataframe_dtypes,
    get_column_names,
    get_metric_names,
)

if TYPE_CHECKING:
    from superset.models.connectors import SqlaTable
    from superset.models.sql_lab import Query


logger = logging.getLogger(__name__)


def _gettext(message: str, **kwargs: Any) -> str:
    if kwargs:
        try:
            return message % kwargs
        except (KeyError, TypeError, ValueError):
            return message
    return message


__ = _gettext


_RESULT_FORMAT_JSON = "json"
_RESULT_FORMAT_CSV = "csv"
_RESULT_FORMAT_XLSX = "xlsx"
_RESULT_FORMATS = frozenset(
    {_RESULT_FORMAT_JSON, _RESULT_FORMAT_CSV, _RESULT_FORMAT_XLSX}
)


def get_column_key(label: tuple[str, ...], metrics: list[str]) -> tuple[Any, ...]:
    """
    Sort columns when combining metrics.

    MultiIndex labels have the metric name as the last element in the
    tuple. We want to sort these according to the list of passed metrics.
    """
    parts: list[Any] = list(label)
    metric = parts[-1]
    parts[-1] = metrics.index(metric)
    return tuple(parts)


def pivot_df(  # pylint: disable=too-many-locals, too-many-arguments, too-many-statements, too-many-branches  # noqa: C901
    df: pd.DataFrame,
    rows: list[str],
    columns: list[str],
    metrics: list[str],
    aggfunc: str = "Sum",
    transpose_pivot: bool = False,
    combine_metrics: bool = False,
    show_rows_total: bool = False,
    show_columns_total: bool = False,
    apply_metrics_on_rows: bool = False,
) -> pd.DataFrame:
    metric_name = __("Total (%(aggfunc)s)", aggfunc=aggfunc)

    if transpose_pivot:
        rows, columns = columns, rows

    if apply_metrics_on_rows:
        rows, columns = columns, rows
        axis = {"columns": 0, "rows": 1}
    else:
        axis = {"columns": 1, "rows": 0}

    df = df.fillna("SUPERSET_PANDAS_NAN")

    if rows or columns:
        df = df.pivot_table(
            index=rows,
            columns=columns,
            values=metrics,
            aggfunc=pivot_v2_aggfunc_map[aggfunc],
            margins=False,
        )
    else:
        # if there's no rows nor columns we have a single value; update
        # the index with the metric name so it shows up in the table
        df.index = pd.Index([*df.index[:-1], metric_name], name="metric")

    if columns and not rows:
        df = df.stack()
        if not isinstance(df, pd.DataFrame):
            df = df.to_frame()
        df = df.T
        df = df[metrics]
        df.index = pd.Index([*df.index[:-1], metric_name], name="metric")

    if combine_metrics and isinstance(df.columns, pd.MultiIndex):
        new_order = [*range(1, df.columns.nlevels), 0]
        df = df.reorder_levels(new_order, axis=1)

        decorated_columns = [(col, i) for i, col in enumerate(df.columns)]
        grouped_columns = sorted(
            decorated_columns, key=lambda t: get_column_key(t[0], metrics)
        )
        indexes = [i for col, i in grouped_columns]
        df = df[df.columns[indexes]]
    elif rows:
        df = df[metrics]

    if aggfunc.endswith(" as Fraction of Total"):
        total = df.sum().sum()
        df = df.astype(total.dtypes) / total
    elif aggfunc.endswith(" as Fraction of Columns"):
        total = df.sum(axis=axis["rows"])
        df = df.astype(total.dtypes).div(total, axis=axis["columns"])
    elif aggfunc.endswith(" as Fraction of Rows"):
        total = df.sum(axis=axis["columns"])
        df = df.astype(total.dtypes).div(total, axis=axis["rows"])

    if not isinstance(df.index, pd.MultiIndex):
        df.index = pd.MultiIndex.from_tuples([(str(i),) for i in df.index])
    if not isinstance(df.columns, pd.MultiIndex):
        df.columns = pd.MultiIndex.from_tuples([(str(i),) for i in df.columns])

    if show_rows_total:
        groups = df.columns
        if not apply_metrics_on_rows:
            for col in df.columns:
                if pd.api.types.is_numeric_dtype(df[col]):
                    df[col].replace("SUPERSET_PANDAS_NAN", np.nan, inplace=True)
                else:
                    df[col].replace("SUPERSET_PANDAS_NAN", "nan", inplace=True)
        else:
            df.replace("SUPERSET_PANDAS_NAN", np.nan, inplace=True)
        for level in range(df.columns.nlevels):
            subgroups = {group[:level] for group in groups}
            for subgroup in subgroups:
                slice_ = df.columns.get_loc(subgroup)
                subtotal = pivot_v2_aggfunc_map[aggfunc](df.iloc[:, slice_], axis=1)
                depth = df.columns.nlevels - len(subgroup) - 1
                total = metric_name if level == 0 else __("Subtotal")
                subtotal_name = tuple([*subgroup, total, *([""] * depth)])  # noqa: C409
                # insert column after subgroup
                df.insert(int(slice_.stop), subtotal_name, subtotal)

    if rows and show_columns_total:
        groups = df.index
        for level in range(df.index.nlevels):
            subgroups = {group[:level] for group in groups}
            for subgroup in subgroups:
                try:
                    slice_ = df.index.get_loc(subgroup)
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "Error getting location for subgroup %s from %s",
                        subgroup,
                        groups,
                    )
                    raise

                subtotal = pivot_v2_aggfunc_map[aggfunc](
                    df.iloc[slice_, :].apply(pd.to_numeric, errors="coerce"), axis=0
                )
                depth = groups.nlevels - len(subgroup) - 1
                total = metric_name if level == 0 else __("Subtotal")
                subtotal.name = tuple([*subgroup, total, *([""] * depth)])  # noqa: C409
                # insert row after subgroup
                df = pd.concat(
                    [df[: slice_.stop], subtotal.to_frame().T, df[slice_.stop :]]
                )

    if apply_metrics_on_rows:
        df = df.T

    df.replace("SUPERSET_PANDAS_NAN", np.nan, inplace=True)
    df.rename(
        index={"SUPERSET_PANDAS_NAN": np.nan},
        columns={"SUPERSET_PANDAS_NAN": np.nan},
        inplace=True,
    )

    return df


def list_unique_values(series: pd.Series) -> str:
    return ", ".join({str(v) for v in pd.Series.unique(series)})


pivot_v2_aggfunc_map: dict[str, Callable[..., Any]] = {
    "Count": pd.Series.count,
    "Count Unique Values": pd.Series.nunique,
    "List Unique Values": list_unique_values,
    "Sum": pd.Series.sum,
    "Average": pd.Series.mean,
    "Median": pd.Series.median,
    "Sample Variance": lambda series: pd.series.var(series) if len(series) > 1 else 0,
    # The trailing comma makes this a 1-tuple (a known upstream wart);
    # kept verbatim for parity.
    "Sample Standard Deviation": (  # type: ignore[dict-item]
        lambda series: pd.series.std(series) if len(series) > 1 else 0,
    ),
    "Minimum": pd.Series.min,
    "Maximum": pd.Series.max,
    "First": lambda series: series[:1],
    "Last": lambda series: series[-1:],
    "Sum as Fraction of Total": pd.Series.sum,
    "Sum as Fraction of Rows": pd.Series.sum,
    "Sum as Fraction of Columns": pd.Series.sum,
    "Count as Fraction of Total": pd.Series.count,
    "Count as Fraction of Rows": pd.Series.count,
    "Count as Fraction of Columns": pd.Series.count,
}


def pivot_table_v2(
    df: pd.DataFrame,
    form_data: dict[str, Any],
    datasource: Optional[Union["SqlaTable", "Query"]] = None,
) -> pd.DataFrame:
    verbose_map = datasource.data["verbose_map"] if datasource else None

    return pivot_df(
        df,
        rows=get_column_names(form_data.get("groupbyRows"), verbose_map),
        columns=get_column_names(form_data.get("groupbyColumns"), verbose_map),
        metrics=get_metric_names(form_data["metrics"], verbose_map),
        aggfunc=form_data.get("aggregateFunction", "Sum"),
        transpose_pivot=bool(form_data.get("transposePivot")),
        combine_metrics=bool(form_data.get("combineMetric")),
        show_rows_total=bool(form_data.get("rowTotals")),
        show_columns_total=bool(form_data.get("colTotals")),
        apply_metrics_on_rows=form_data.get("metricsLayout") == "ROWS",
    )


def table(
    df: pd.DataFrame,
    form_data: dict[str, Any],
    datasource: Optional[  # pylint: disable=unused-argument
        Union["SqlaTable", "Query"]
    ] = None,
) -> pd.DataFrame:
    column_config = form_data.get("column_config", {})
    for column, config in column_config.items():
        if "d3NumberFormat" in config:
            format_ = "{:" + config["d3NumberFormat"] + "}"
            try:
                df[column] = df[column].apply(format_.format)
            except Exception:  # pylint: disable=broad-except  # noqa: S110
                # if we can't format the column for any reason, send as is
                pass

    return df


post_processors: dict[str, Callable[..., pd.DataFrame]] = {
    "pivot_table_v2": pivot_table_v2,
    "table": table,
}


def apply_client_processing(  # noqa: C901
    result: dict[Any, Any],
    form_data: Optional[dict[str, Any]] = None,
    datasource: Optional[Union["SqlaTable", "Query"]] = None,
) -> dict[Any, Any]:
    try:
        from superset.events import event_logger

        event_logger.log_with_context(action="apply_client_processing")
    except Exception:  # noqa: BLE001
        logger.debug("Failed to audit-log apply_client_processing", exc_info=True)
    form_data = form_data or {}

    viz_type = form_data.get("viz_type")
    if viz_type not in post_processors:
        return result

    post_processor = post_processors[viz_type]

    for query in result["queries"]:
        if query["result_format"] not in _RESULT_FORMATS:
            raise Exception(  # pylint: disable=broad-exception-raised
                f"Result format {query['result_format']} not supported"
            )

        data = query["data"]

        if isinstance(data, str):
            data = data.strip()

        if not data:
            continue

        if query["result_format"] == _RESULT_FORMAT_JSON:
            df = pd.DataFrame.from_dict(data)
        elif query["result_format"] == _RESULT_FORMAT_CSV:
            df = pd.read_csv(StringIO(data))

        if datasource:
            df.rename(columns=datasource.data["verbose_map"], inplace=True)

        processed_df = post_processor(df, form_data, datasource)

        query["colnames"] = list(processed_df.columns)
        query["indexnames"] = list(processed_df.index)
        query["coltypes"] = extract_dataframe_dtypes(processed_df, datasource)
        query["rowcount"] = len(processed_df.index)

        show_default_index = not isinstance(processed_df.index, pd.RangeIndex)

        processed_df.columns = [
            (
                " ".join(str(name) for name in column).strip()
                if isinstance(column, tuple)
                else column
            )
            for column in processed_df.columns
        ]
        processed_df.index = [
            (
                " ".join(str(name) for name in index).strip()
                if isinstance(index, tuple)
                else index
            )
            for index in processed_df.index
        ]

        if query["result_format"] == _RESULT_FORMAT_JSON:
            query["data"] = processed_df.to_dict()
        elif query["result_format"] == _RESULT_FORMAT_CSV:
            buf = StringIO()
            processed_df.to_csv(buf, index=show_default_index)
            buf.seek(0)
            query["data"] = buf.getvalue()

    return result
