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
from typing import Any, Optional, Union

from pandas import DataFrame

from superset.exceptions import InvalidPostProcessingError
from superset.utils.pandas_postprocessing.utils import (
    _append_columns,
    DENYLIST_ROLLING_FUNCTIONS,
    validate_column_args,
)


@validate_column_args("columns")
def rolling(  # pylint: disable=too-many-arguments
    df: DataFrame,
    rolling_type: str,
    columns: dict[str, str],
    window: Optional[int] = None,
    rolling_type_options: Optional[dict[str, Any]] = None,
    center: bool = False,
    win_type: Optional[str] = None,
    min_periods: Optional[int] = None,
) -> DataFrame:
    """Apply a rolling window on the dataset."""
    rolling_type_options = rolling_type_options or {}
    df_rolling = df.loc[:, columns.keys()]

    kwargs: dict[str, Union[str, int]] = {}
    if window is None:
        raise InvalidPostProcessingError("Undefined window for rolling operation")
    if window == 0:
        raise InvalidPostProcessingError("Window must be > 0")

    kwargs["window"] = window
    if min_periods is not None:
        kwargs["min_periods"] = min_periods
    if center is not None:
        kwargs["center"] = center
    if win_type is not None:
        kwargs["win_type"] = win_type

    df_rolling = df_rolling.rolling(**kwargs)
    if rolling_type not in DENYLIST_ROLLING_FUNCTIONS or not hasattr(
        df_rolling, rolling_type
    ):
        raise InvalidPostProcessingError(f"Invalid rolling_type: {rolling_type}")
    try:
        df_rolling = getattr(df_rolling, rolling_type)(**rolling_type_options)
    except TypeError as ex:
        raise InvalidPostProcessingError(
            f"Invalid options for {rolling_type}: {rolling_type_options}"
        ) from ex

    df_rolling = _append_columns(df, df_rolling, columns)

    if min_periods:
        df_rolling = df_rolling[min_periods - 1 :]
    return df_rolling
