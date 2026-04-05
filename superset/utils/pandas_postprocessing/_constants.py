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
"""Constants and enums used by pandas post-processing functions.

Ported from ``superset.utils.core`` and ``superset.constants`` so that the
superset pandas_postprocessing package has zero superset imports.
"""

from __future__ import annotations

from enum import Enum, StrEnum

# ---------------------------------------------------------------------------
# From superset.utils.core
# ---------------------------------------------------------------------------

DTTM_ALIAS = "__timestamp"
TIME_COMPARISON = "__"


class PostProcessingBoxplotWhiskerType(StrEnum):
    TUKEY = "tukey"
    MINMAX = "min/max"
    PERCENTILE = "percentile"


class PostProcessingContributionOrientation(StrEnum):
    ROW = "row"
    COLUMN = "column"


# ---------------------------------------------------------------------------
# From superset.constants
# ---------------------------------------------------------------------------

NULL_STRING = "<NULL>"


class TimeGrain(StrEnum):
    SECOND = "PT1S"
    FIVE_SECONDS = "PT5S"
    THIRTY_SECONDS = "PT30S"
    MINUTE = "PT1M"
    FIVE_MINUTES = "PT5M"
    TEN_MINUTES = "PT10M"
    FIFTEEN_MINUTES = "PT15M"
    THIRTY_MINUTES = "PT30M"
    HALF_HOUR = "PT0.5H"
    HOUR = "PT1H"
    SIX_HOURS = "PT6H"
    DAY = "P1D"
    WEEK = "P1W"
    WEEK_STARTING_SUNDAY = "1969-12-28T00:00:00Z/P1W"
    WEEK_STARTING_MONDAY = "1969-12-29T00:00:00Z/P1W"
    WEEK_ENDING_SATURDAY = "P1W/1970-01-03T00:00:00Z"
    WEEK_ENDING_SUNDAY = "P1W/1970-01-04T00:00:00Z"
    MONTH = "P1M"
    QUARTER = "P3M"
    QUARTER_YEAR = "P0.25Y"
    YEAR = "P1Y"


class PandasAxis(int, Enum):
    ROW = 0
    COLUMN = 1


class PandasPostprocessingCompare(StrEnum):
    DIFF = "difference"
    PCT = "percentage"
    RAT = "ratio"
