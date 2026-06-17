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
"""``/api/v1/query`` and ``/api/v1/explore/form_data`` are NOT deprecated stubs —
they are real, functional endpoints served by ``QueryController`` and
``ExploreFormDataController`` respectively. ``LegacyApiController`` only hosts
the still-active ``/api/v1/time_range/`` endpoint.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import prison
import pytest

from superset.controllers.explore_form_data import ExploreFormDataController
from superset.controllers.legacy_api import LegacyApiController
from superset.controllers.query import QueryController
from superset.exceptions import SupersetValidationException

_time_range = LegacyApiController.time_range.fn


async def test_time_range_with_valid_q() -> None:
    request = MagicMock()
    request.query_params = {"q": prison.dumps("Last week")}

    response = await _time_range(None, request=request, current_user=MagicMock())
    assert response.status_code == 200
    result = response.content["result"]
    assert len(result) == 1
    assert result[0]["timeRange"] == "Last week"
    assert "since" in result[0]
    assert "until" in result[0]


async def test_time_range_missing_q() -> None:
    request = MagicMock()
    request.query_params = {}

    with pytest.raises(SupersetValidationException):
        await _time_range(None, request=request, current_user=MagicMock())


def test_query_is_a_real_endpoint() -> None:
    assert QueryController.path == "/api/v1/query"


def test_form_data_is_a_real_endpoint() -> None:
    assert ExploreFormDataController.path == "/api/v1/explore/form_data"


def test_legacy_controller_path() -> None:
    assert LegacyApiController.path == "/api/v1"
