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
from unittest.mock import MagicMock

import prison
import pytest

from liteset.params.rison import provide_rison_query


def _make_request(params: dict) -> MagicMock:
    req = MagicMock()
    req.query_params = params
    return req


async def test_decode_valid():
    encoded = prison.dumps({"filters": [], "page": 0})
    req = _make_request({"q": encoded})
    result = await provide_rison_query(req)
    assert result == {"filters": [], "page": 0}


async def test_absent_returns_none():
    req = _make_request({})
    assert await provide_rison_query(req) is None


async def test_invalid_raises():
    from liteset.exceptions import LitesetValidationException

    req = _make_request({"q": "((broken}}"})
    with pytest.raises(LitesetValidationException):
        await provide_rison_query(req)
