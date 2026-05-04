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
"""Superset utilities for ``pandas.DataFrame``.

Ported 1:1 from ``superset_old/dataframe.py``.  Used by ``sql_lab``,
``viz`` and ``views/utils`` to convert in-memory result frames into
plain JSON records while preserving the JS-safe big-integer cast that
the front-end relies on (any integer larger than ``JS_MAX_INTEGER``
loses precision once it crosses the JSON boundary into the browser).
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from superset.utils.core import JS_MAX_INTEGER

logger = logging.getLogger(__name__)


def _convert_big_integers(val: Any) -> Any:
    """Cast integers larger than ``JS_MAX_INTEGER`` to strings.

    :param val: the value to process
    :returns: ``val`` itself unless it's an int with absolute value over
        ``JS_MAX_INTEGER``, in which case the stringified form is
        returned so the front-end doesn't silently lose precision.
    """
    return str(val) if isinstance(val, int) and abs(val) > JS_MAX_INTEGER else val


def df_to_records(dframe: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a DataFrame to a list of records (dicts).

    Each integer cell is run through :func:`_convert_big_integers` so
    that bigint values survive the JSON round-trip to the browser
    without losing precision.

    :param dframe: the DataFrame to convert
    :returns: a list of dictionaries reflecting each row of the DataFrame
    """
    if not dframe.columns.is_unique:
        logger.warning(
            "DataFrame columns are not unique, some columns will be omitted."
        )
    records = dframe.to_dict(orient="records")

    for record in records:
        for key in record:
            record[key] = _convert_big_integers(record[key])

    return records
