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
"""Tests for the worker's datasource-id resolution.

The chart-data async submit serialises ``datasource`` as a dict
``{"id": N, "type": "table"}``; the worker must extract the id from that
(as well as the legacy ``"N__table"`` string form) so it loads the real
datasource — otherwise RLS composition fails on a ``None`` datasource.
"""

from __future__ import annotations

import pytest

from superset.tasks.async_queries import _resolve_datasource_id


@pytest.mark.parametrize(
    "form_data, expected",
    [
        ({"datasource": {"id": 1, "type": "table"}}, 1),  # query-context dict form
        ({"datasource": {"id": "7", "type": "table"}}, 7),  # stringified int id
        ({"datasource": "5__table"}, 5),  # legacy explore string form
        ({"datasource": "12__query"}, 12),
        ({"datasource": ""}, None),  # empty
        ({}, None),  # missing
        ({"datasource": {"type": "table"}}, None),  # dict without id
        ({"datasource": {"id": None}}, None),  # explicit None id
        ({"datasource": {"id": "abc"}}, None),  # non-numeric id
        ({"datasource": "not-a-ref"}, None),  # string without "__"
    ],
)
def test_resolve_datasource_id(form_data, expected):
    assert _resolve_datasource_id(form_data) == expected
