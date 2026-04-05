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

from superset.exceptions import InvalidPostProcessingError
from superset.utils.pandas_postprocessing.utils import (
    _append_columns,
    ALLOWLIST_CUMULATIVE_FUNCTIONS,
    validate_column_args,
)


@validate_column_args("columns")
def cum(
    df: DataFrame,
    operator: str,
    columns: dict[str, str],
) -> DataFrame:
    """
    Calculate cumulative sum/product/min/max for select columns.
    """
    columns = columns or {}
    df_cum = df.loc[:, columns.keys()]
    df_cum = df_cum.fillna(0)
    operation = "cum" + operator
    if operation not in ALLOWLIST_CUMULATIVE_FUNCTIONS or not hasattr(
        df_cum, operation
    ):
        raise InvalidPostProcessingError(f"Invalid cumulative operator: {operator}")
    df_cum = _append_columns(df, getattr(df_cum, operation)(), columns)
    return df_cum
