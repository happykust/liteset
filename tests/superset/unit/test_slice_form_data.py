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
"""Regression for ``Slice.form_data`` / ``params_dict`` with non-object params.

``params`` is an unvalidated JSON-string column. A valid-but-non-object value
(``"[1,2]"`` / ``"\\"s\\""`` / ``"5"``) made ``form_data`` do
``dict.update`` on a parsed list/str → ``AttributeError`` → HTTP 500. Because
the chart LIST endpoint builds ``form_data`` for every row, a single bad chart
500'd the whole list (live-probed). ``form_data`` must coerce a non-object
parse to ``{}`` (matching ``params_dict``).
"""

from __future__ import annotations

import pytest

from superset.models.slice import Slice


def _slice(params: str | None) -> Slice:
    return Slice(
        id=1,
        slice_name="t",
        viz_type="table",
        datasource_id=1,
        datasource_type="table",
        params=params,
    )


@pytest.mark.parametrize("params", ["[1, 2, 3]", '"a string"', "5", "true", "null"])
def test_form_data_tolerates_non_object_params(params: str) -> None:
    fd = _slice(params).form_data
    assert isinstance(fd, dict)
    # The slice identifiers are always merged in.
    assert fd["slice_id"] == 1
    assert fd["viz_type"] == "table"
    assert fd["datasource"] == "1__table"


@pytest.mark.parametrize("params", ["[1, 2, 3]", '"a string"', "5", "not json"])
def test_params_dict_tolerates_non_object_params(params: str) -> None:
    assert _slice(params).params_dict == {}


def test_form_data_with_object_params_preserved() -> None:
    fd = _slice('{"row_limit": 100}').form_data
    assert fd["row_limit"] == 100
    assert fd["slice_id"] == 1
