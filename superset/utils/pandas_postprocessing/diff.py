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
from pandas import DataFrame

from superset.utils.pandas_postprocessing._constants import PandasAxis
from superset.utils.pandas_postprocessing.utils import (
    _append_columns,
    validate_column_args,
)


@validate_column_args("columns")
def diff(
    df: DataFrame,
    columns: dict[str, str],
    periods: int = 1,
    axis: PandasAxis = PandasAxis.ROW,
) -> DataFrame:
    """
    Calculate row-by-row or column-by-column difference for select columns.
    """
    df_diff = df[columns.keys()]
    df_diff = df_diff.diff(periods=periods, axis=axis)
    return _append_columns(df, df_diff, columns)
