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
from typing import Optional, Union

import pandas as pd

from superset.exceptions import InvalidPostProcessingError
from superset.utils.pandas_postprocessing.utils import RESAMPLE_METHOD


def resample(
    df: pd.DataFrame,
    rule: str,
    method: str,
    fill_value: Optional[Union[float, int]] = None,
) -> pd.DataFrame:
    """Support upsampling in resample."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise InvalidPostProcessingError("Resample operation requires DatetimeIndex")
    if method not in RESAMPLE_METHOD:
        raise InvalidPostProcessingError(
            "Resample method should be in " + ", ".join(RESAMPLE_METHOD) + "."
        )

    if method == "asfreq" and fill_value is not None:
        _df = df.resample(rule).asfreq(fill_value=fill_value)
        _df = _df.fillna(fill_value)
    elif method == "linear":
        _df = df.resample(rule).interpolate()
    else:
        _df = getattr(df.resample(rule), method)()
        if method in ("ffill", "bfill"):
            _df = _df.fillna(method=method)
    return _df
