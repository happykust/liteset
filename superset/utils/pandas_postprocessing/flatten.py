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
from collections.abc import Iterable, Sequence
from typing import Any, Union

import pandas as pd

from superset.utils.pandas_postprocessing.utils import (
    _is_multi_index_on_columns,
    escape_separator,
    FLAT_COLUMN_SEPARATOR,
)


def is_sequence(seq: Any) -> bool:
    if isinstance(seq, str):
        return False

    return isinstance(seq, Iterable)


def flatten(
    df: pd.DataFrame,
    reset_index: bool = True,
    drop_levels: Union[Sequence[int], Sequence[str]] = (),
) -> pd.DataFrame:
    """
    Convert N-dimensional DataFrame to a flat DataFrame.
    """
    if _is_multi_index_on_columns(df):
        df.columns = df.columns.droplevel(drop_levels)
        _columns = []
        for series in df.columns.to_flat_index():
            _cells = []
            for cell in series if is_sequence(series) else [series]:
                if pd.notnull(cell):
                    _cells.append(escape_separator(str(cell)))
            _columns.append(FLAT_COLUMN_SEPARATOR.join(_cells))

        df.columns = _columns

    if reset_index and not isinstance(df.index, pd.RangeIndex):
        df = df.reset_index(level=0)
    return df
