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
from typing import Any, Optional

from pandas import DataFrame

from superset.exceptions import InvalidPostProcessingError
from superset.utils.pandas_postprocessing._constants import NULL_STRING, PandasAxis
from superset.utils.pandas_postprocessing.utils import (
    _get_aggregate_funcs,
    validate_column_args,
)


@validate_column_args("index", "columns")
def pivot(  # pylint: disable=too-many-arguments
    df: DataFrame,
    index: list[str],
    aggregates: dict[str, dict[str, Any]],
    columns: Optional[list[str]] = None,
    metric_fill_value: Optional[Any] = None,
    column_fill_value: Optional[str] = NULL_STRING,
    drop_missing_columns: Optional[bool] = True,
    combine_value_with_metric: bool = False,
    marginal_distributions: Optional[bool] = None,
    marginal_distribution_name: Optional[str] = None,
) -> DataFrame:
    """Perform a pivot operation on a DataFrame."""
    if not index:
        raise InvalidPostProcessingError(
            "Pivot operation requires at least one index"
        )
    if not aggregates:
        raise InvalidPostProcessingError(
            "Pivot operation must include at least one aggregate"
        )

    if columns and column_fill_value:
        df[columns] = df[columns].fillna(value=column_fill_value)

    aggregate_funcs = _get_aggregate_funcs(df, aggregates)

    aggfunc = {na.column: na.aggfunc for na in aggregate_funcs.values()}

    series_set = set()
    if not drop_missing_columns and columns:
        for row in df[columns].itertuples():
            for metric in aggfunc.keys():
                series_set.add(tuple([metric]) + tuple(row[1:]))  # noqa: C409

    df = df.pivot_table(
        values=aggfunc.keys(),
        index=index,
        columns=columns,
        aggfunc=aggfunc,
        fill_value=metric_fill_value,
        dropna=drop_missing_columns,
        margins=marginal_distributions,
        margins_name=marginal_distribution_name,
    )

    if not drop_missing_columns and len(series_set) > 0 and not df.empty:
        df = df.drop(df.columns.difference(series_set), axis=PandasAxis.COLUMN)

    if combine_value_with_metric:
        df = df.stack(0).unstack()

    return df
