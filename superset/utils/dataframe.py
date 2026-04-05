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
"""DataFrame processing utilities."""

from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from superset.utils.date import DateColumn, parse_human_timedelta

logger = logging.getLogger(__name__)


def df_metrics_to_num(df: pd.DataFrame, metric_names: list[str]) -> None:
    """Convert metric columns to numeric when pandas cannot auto-detect."""
    for col, dtype in df.dtypes.items():
        if dtype.type == np.object_ and col in metric_names:
            df[col] = df[col].infer_objects()


def normalize_dttm_col(
    df: pd.DataFrame,
    dttm_cols: tuple[DateColumn, ...] = (),
) -> None:
    """Normalize datetime columns in a DataFrame.

    Handles epoch_s/epoch_ms conversion, timestamp format parsing,
    offset adjustments, and time_shift adjustments.
    """
    for _col in dttm_cols:
        if _col.col_label not in df.columns:
            continue

        if _col.timestamp_format in ("epoch_s", "epoch_ms"):
            dttm_series = df[_col.col_label]
            if pd.api.types.is_numeric_dtype(dttm_series):
                # Column is formatted as a numeric value
                unit = _col.timestamp_format.replace("epoch_", "")
                df[_col.col_label] = pd.to_datetime(
                    dttm_series,
                    utc=False,
                    unit=unit,
                    origin="unix",
                    errors="coerce",
                    exact=False,
                )
            else:
                # Column has already been formatted as a timestamp.
                try:
                    df[_col.col_label] = dttm_series.apply(
                        lambda x: pd.Timestamp(x) if pd.notna(x) else pd.NaT
                    )
                except ValueError:
                    logger.warning("Unable to convert %s to datetime", _col.col_label)
        else:
            df[_col.col_label] = pd.to_datetime(
                df[_col.col_label],
                utc=False,
                format=_col.timestamp_format,
                errors="coerce",
                exact=False,
            )

        if _col.offset:
            df[_col.col_label] += timedelta(hours=_col.offset)
        if _col.time_shift is not None:
            df[_col.col_label] += parse_human_timedelta(_col.time_shift)
