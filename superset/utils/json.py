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
"""JSON serialization utilities — superset-local replacement for superset.utils.json."""

from __future__ import annotations

import datetime
import decimal
import json
import math
import uuid
from datetime import date, time, timedelta
from typing import Any

import numpy as np
import pytz

# ---------------------------------------------------------------------------
# Epoch helpers (ported from superset.utils.dates)
# ---------------------------------------------------------------------------

EPOCH = datetime.datetime(1970, 1, 1)


def datetime_to_epoch(dttm: datetime.datetime) -> float:
    """Convert datetime to milliseconds since epoch."""
    if dttm.tzinfo:
        dttm = dttm.replace(tzinfo=pytz.utc)
        epoch_with_tz = pytz.utc.localize(EPOCH)
        return (dttm - epoch_with_tz).total_seconds() * 1000
    return (dttm - EPOCH).total_seconds() * 1000


# ---------------------------------------------------------------------------
# base_json_conv  (ported from superset.utils.json)
# ---------------------------------------------------------------------------


def format_timedelta(time_delta: timedelta) -> str:
    """Ensure negative timedeltas are human-readable."""
    if time_delta < timedelta(0):
        return "-" + str(abs(time_delta))
    return str(time_delta)


def base_json_conv(obj: Any) -> Any:  # noqa: C901
    """
    Convert additional types to JSON-compatible forms.

    Handles numpy types, memoryview, Decimal, UUID, bytes, timedelta, set, time.

    :param obj: The serializable object
    :returns: The JSON compatible form
    :raises TypeError: If the object cannot be serialized
    """
    if isinstance(obj, memoryview):
        obj = obj.tobytes()
    if isinstance(obj, np.int64):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (uuid.UUID, time)):
        return str(obj)
    if isinstance(obj, timedelta):
        return format_timedelta(obj)
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            try:
                return obj.decode("utf-16")
            except Exception:
                return "[bytes]"

    raise TypeError(f"Unserializable object {obj} of type {type(obj)}")


# ---------------------------------------------------------------------------
# json_int_dttm_ser  (ported from superset.utils.json)
# ---------------------------------------------------------------------------


def json_int_dttm_ser(obj: Any) -> Any:
    """
    JSON serializer that converts dates to epoch milliseconds.

        >>> json.dumps(
        ...     {'dttm': datetime.datetime(1970, 1, 1)},
        ...     default=json_int_dttm_ser,
        ... )
        '{"dttm": 0.0}'

    :param obj: The serializable object
    :returns: The JSON compatible form
    :raises TypeError: If the object cannot be serialized
    """
    if isinstance(obj, datetime.datetime):
        return datetime_to_epoch(obj)

    if isinstance(obj, date):
        return (obj - EPOCH.date()).total_seconds() * 1000

    return base_json_conv(obj)


# ---------------------------------------------------------------------------
# Generic default serializer (used by dumps/loads below)
# ---------------------------------------------------------------------------


def _default_serializer(obj: Any) -> Any:
    """Handle datetime, UUID, Decimal, numpy types, NaN, set, timedelta, time."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, timedelta):
        return str(obj)
    if isinstance(obj, time):
        return str(obj)
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.bool_):
        return bool(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def dumps(obj: Any, **kwargs: Any) -> str:
    kwargs.setdefault("default", _default_serializer)
    return json.dumps(obj, **kwargs)


def loads(s: str | bytes, **kwargs: Any) -> Any:
    return json.loads(s, **kwargs)
