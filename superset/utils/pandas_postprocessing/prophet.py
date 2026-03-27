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

import contextlib
import logging
from typing import Iterator, Optional, Union

import pandas as pd
from pandas import DataFrame

from superset.exceptions import InvalidPostProcessingError
from superset.utils.pandas_postprocessing._constants import DTTM_ALIAS
from superset.utils.pandas_postprocessing.utils import PROPHET_TIME_GRAIN_MAP


@contextlib.contextmanager
def _suppress_logging(
    logger_name: str | None = None,
    new_level: int = logging.CRITICAL,
) -> Iterator[None]:
    """Context manager to suppress logging during the execution of code block."""
    target_logger = logging.getLogger(logger_name)
    original_level = target_logger.getEffectiveLevel()
    target_logger.setLevel(new_level)
    try:
        yield
    finally:
        target_logger.setLevel(original_level)


def _prophet_parse_seasonality(
    input_value: Optional[Union[bool, int]],
) -> Union[bool, str, int]:
    if input_value is None:
        return "auto"
    if isinstance(input_value, bool):
        return input_value
    try:
        return int(input_value)
    except ValueError:
        return input_value


def _prophet_fit_and_predict(  # pylint: disable=too-many-arguments
    df: DataFrame,
    confidence_interval: float,
    yearly_seasonality: Union[bool, str, int],
    weekly_seasonality: Union[bool, str, int],
    daily_seasonality: Union[bool, str, int],
    periods: int,
    freq: str,
) -> DataFrame:
    """Fit a prophet model and return a DataFrame with predicted results."""
    try:
        with _suppress_logging("prophet.plot"):
            from prophet import Prophet  # pylint: disable=import-outside-toplevel

        prophet_logger = logging.getLogger("prophet.plot")
        prophet_logger.setLevel(logging.CRITICAL)
        prophet_logger.setLevel(logging.NOTSET)
    except ModuleNotFoundError as ex:
        raise InvalidPostProcessingError("`prophet` package not installed") from ex
    model = Prophet(
        interval_width=confidence_interval,
        yearly_seasonality=yearly_seasonality,
        weekly_seasonality=weekly_seasonality,
        daily_seasonality=daily_seasonality,
    )
    if df["ds"].dt.tz:
        df["ds"] = df["ds"].dt.tz_convert(None)
    model.fit(df)
    future = model.make_future_dataframe(periods=periods, freq=freq)
    forecast = model.predict(future)[["ds", "yhat", "yhat_lower", "yhat_upper"]]
    return forecast.join(df.set_index("ds"), on="ds").set_index(["ds"])


def prophet(  # pylint: disable=too-many-arguments
    df: DataFrame,
    time_grain: str,
    periods: int,
    confidence_interval: float,
    yearly_seasonality: Optional[Union[bool, int]] = None,
    weekly_seasonality: Optional[Union[bool, int]] = None,
    daily_seasonality: Optional[Union[bool, int]] = None,
    index: Optional[str] = None,
) -> DataFrame:
    """
    Add forecasts to each series in a timeseries dataframe, along with confidence
    intervals for the prediction.
    """
    index = index or DTTM_ALIAS
    if not time_grain:
        raise InvalidPostProcessingError("Time grain missing")
    if time_grain not in PROPHET_TIME_GRAIN_MAP:
        raise InvalidPostProcessingError(
            f"Unsupported time grain: {time_grain}"
        )
    freq = PROPHET_TIME_GRAIN_MAP[time_grain]
    if not isinstance(periods, int) or periods < 0:
        raise InvalidPostProcessingError("Periods must be a whole number")
    if not confidence_interval or confidence_interval <= 0 or confidence_interval >= 1:
        raise InvalidPostProcessingError(
            "Confidence interval must be between 0 and 1 (exclusive)"
        )
    if index not in df.columns:
        raise InvalidPostProcessingError("DataFrame must include temporal column")
    if len(df.columns) < 2:
        raise InvalidPostProcessingError("DataFrame include at least one series")

    target_df = DataFrame()

    for column in [
        column
        for column in df.columns
        if column != index
        and pd.to_numeric(df[column], errors="coerce").notnull().all()
    ]:
        fit_df = _prophet_fit_and_predict(
            df=df[[index, column]].rename(columns={index: "ds", column: "y"}),
            confidence_interval=confidence_interval,
            yearly_seasonality=_prophet_parse_seasonality(yearly_seasonality),
            weekly_seasonality=_prophet_parse_seasonality(weekly_seasonality),
            daily_seasonality=_prophet_parse_seasonality(daily_seasonality),
            periods=periods,
            freq=freq,
        )
        new_columns = [
            f"{column}__yhat",
            f"{column}__yhat_lower",
            f"{column}__yhat_upper",
            f"{column}",
        ]
        fit_df.columns = new_columns
        if target_df.empty:
            target_df = fit_df
        else:
            for new_column in new_columns:
                target_df = target_df.assign(**{new_column: fit_df[new_column]})
    target_df.reset_index(level=0, inplace=True)
    return target_df.rename(columns={"ds": index})
