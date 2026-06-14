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
"""Flask-free port of the vendored upstream
``tests/unit_tests/common/test_time_range_utils.py``.

Liteset moved ``get_since_until_from_time_range`` out of
``superset.common.utils.time_range_utils`` into ``superset.utils.date`` and
reads the relative-time defaults from ``SupersetSettings`` instead of
``current_app.config``.  ``EvalDateTruncFunc`` also lives in
``superset.utils.date`` (not ``superset.utils.date_parser``), so the mock
target is updated accordingly.

Liteset has no standalone ``get_since_until_from_query_object`` helper: the
upstream logic (``query_object.time_range`` takes precedence, otherwise scan
the adhoc filters for the last ``TEMPORAL_RANGE`` entry) is inlined into
``AsyncQueryObject.__post_init__``, which resolves ``from_dttm``/``to_dttm``.
The two upstream query-object cases are therefore ported against that real
code path: instead of the Flask-bound ``dummy_query_object`` fixture +
``QueryObjectFactory``, they construct an ``AsyncQueryObject`` directly and
assert its computed ``(from_dttm, to_dttm)`` tuple — the exact upstream
expected values.
"""

from datetime import datetime
from unittest import mock

from superset.common.query_object import AsyncQueryObject
from superset.utils.date import get_since_until_from_time_range


def test__get_since_until_from_time_range():
    assert get_since_until_from_time_range(time_range="2001 : 2002") == (
        datetime(2001, 1, 1),
        datetime(2002, 1, 1),
    )
    assert get_since_until_from_time_range(
        time_range="2001 : 2002", time_shift="8 hours ago"
    ) == (
        datetime(2000, 12, 31, 16, 0, 0),
        datetime(2001, 12, 31, 16, 0, 0),
    )
    with mock.patch(
        "superset.utils.date.EvalDateTruncFunc.eval",
        return_value=datetime(2000, 1, 1, 0, 0, 0),
    ):
        assert (
            get_since_until_from_time_range(
                time_range="Last year",
                extras={
                    "relative_end": "2100",
                },
            )
        )[1] == datetime(2100, 1, 1, 0, 0)
    with mock.patch(
        "superset.utils.date.EvalDateTruncFunc.eval",
        return_value=datetime(2000, 1, 1, 0, 0, 0),
    ):
        assert (
            get_since_until_from_time_range(
                time_range="Next year",
                extras={
                    "relative_start": "2000",
                },
            )
        )[0] == datetime(2000, 1, 1, 0, 0)


def test__since_until_from_time_range():
    # Upstream ``test__since_until_from_time_range``: a query object carrying
    # ``time_range`` + ``time_shift`` resolves to the shifted (since, until).
    # Liteset inlines ``get_since_until_from_query_object`` into
    # ``AsyncQueryObject.__post_init__`` → assert the computed dttms directly.
    query_object = AsyncQueryObject(
        datasource={},
        time_range="2001 : 2002",
        time_shift="8 hours ago",
    )
    assert (query_object.from_dttm, query_object.to_dttm) == (
        datetime(2000, 12, 31, 16, 0, 0),
        datetime(2001, 12, 31, 16, 0, 0),
    )


def test__since_until_from_adhoc_filters():
    # Upstream ``test__since_until_from_adhoc_filters``: with no top-level
    # ``time_range``, the BASE_AXIS column's ``TEMPORAL_RANGE`` adhoc filter
    # drives (since, until).
    query_object = AsyncQueryObject(
        datasource={},
        filters=[{"col": "dttm", "op": "TEMPORAL_RANGE", "val": "2001 : 2002"}],
        columns=[
            {
                "columnType": "BASE_AXIS",
                "label": "dttm",
                "sqlExpression": "dttm",
            }
        ],
    )
    assert (query_object.from_dttm, query_object.to_dttm) == (
        datetime(2001, 1, 1, 0, 0, 0),
        datetime(2002, 1, 1, 0, 0, 0),
    )
