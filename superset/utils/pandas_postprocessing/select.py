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
from typing import Optional

from pandas import DataFrame

from superset.utils.pandas_postprocessing.utils import validate_column_args


@validate_column_args("columns", "drop", "rename")
def select(
    df: DataFrame,
    columns: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
    rename: Optional[dict[str, str]] = None,
) -> DataFrame:
    """Only select a subset of columns in the original dataset."""
    df_select = df.copy(deep=False)
    if columns:
        df_select = df_select[columns]
    if exclude:
        df_select = df_select.drop(exclude, axis=1)
    if rename is not None:
        df_select = df_select.rename(columns=rename)
    return df_select
