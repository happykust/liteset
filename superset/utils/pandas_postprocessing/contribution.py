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

from decimal import Decimal
from typing import Any

from pandas import DataFrame, MultiIndex

from superset.exceptions import InvalidPostProcessingError
from superset.utils.pandas_postprocessing._constants import (
    PostProcessingContributionOrientation,
)
from superset.utils.pandas_postprocessing.utils import validate_column_args


@validate_column_args("columns")
def contribution(
    df: DataFrame,
    orientation: (
        PostProcessingContributionOrientation | None
    ) = PostProcessingContributionOrientation.COLUMN,
    columns: list[str] | None = None,
    time_shifts: list[str] | None = None,
    rename_columns: list[str] | None = None,
    contribution_totals: dict[str, float] | None = None,
) -> DataFrame:
    """
    Calculate cell contribution to row/column total for numeric columns.
    Non-numeric columns will be kept untouched.
    """
    contribution_df = df.copy()
    numeric_df = contribution_df.select_dtypes(include=["number", Decimal])
    numeric_df.fillna(0, inplace=True)
    if columns:
        numeric_columns = numeric_df.columns.tolist()
        for col in columns:
            if col not in numeric_columns:
                raise InvalidPostProcessingError(
                    f'Column "{col}" is not numeric or does not '
                    "exists in the query results."
                )
    actual_columns = columns or numeric_df.columns

    rename_columns = rename_columns or actual_columns
    if len(rename_columns) != len(actual_columns):
        raise InvalidPostProcessingError(
            "`rename_columns` must have the same length "
            "as `columns` + `time_shift_columns`."
        )
    numeric_df_view = numeric_df[actual_columns]

    if orientation == PostProcessingContributionOrientation.COLUMN:
        if contribution_totals:
            for i, col in enumerate(numeric_df_view.columns):
                total = contribution_totals.get(col)
                rename_col = rename_columns[i]
                if total is None or total == 0:
                    contribution_df[rename_col] = 0
                else:
                    contribution_df[rename_col] = numeric_df_view[col] / total
        else:
            numeric_df_view = numeric_df_view / numeric_df_view.values.sum(
                axis=0, keepdims=True
            )
            contribution_df[rename_columns] = numeric_df_view
        return contribution_df

    result = get_column_groups(numeric_df_view, time_shifts, rename_columns)
    calculate_row_contribution(
        contribution_df, result["non_time_shift"][0], result["non_time_shift"][1]
    )
    for time_shift in result["time_shifts"].items():
        calculate_row_contribution(contribution_df, time_shift[1][0], time_shift[1][1])
    return contribution_df


def get_column_groups(
    df: DataFrame, time_shifts: list[str] | None, rename_columns: list[str]
) -> dict[str, Any]:
    """Group columns based on whether they have a time shift."""
    result: dict[str, Any] = {
        "non_time_shift": ([], []),
        "time_shifts": {},
    }
    for i, col in enumerate(df.columns):
        col_0 = col[0] if isinstance(df.columns, MultiIndex) else col
        time_shift = None
        if time_shifts and isinstance(col_0, str):
            for ts in time_shifts:
                if col_0.endswith(ts):
                    time_shift = ts
                    break
        if time_shift is not None:
            if time_shift not in result["time_shifts"]:
                result["time_shifts"][time_shift] = ([], [])
            result["time_shifts"][time_shift][0].append(col)
            result["time_shifts"][time_shift][1].append(rename_columns[i])
        else:
            result["non_time_shift"][0].append(col)
            result["non_time_shift"][1].append(rename_columns[i])
    return result


def calculate_row_contribution(
    df: DataFrame, columns: list[str], rename_columns: list[str]
) -> None:
    """Calculate the contribution of each column to the row total."""
    row_sum_except_selected = df.loc[:, columns].sum(axis=1)
    df[rename_columns] = df.loc[:, columns].div(row_sum_except_selected, axis=0)
