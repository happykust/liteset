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
import msgspec

from superset.schemas.base import (
    ApiListResponse,
    ApiResponse,
    ErrorResponse,
    SupersetErrorDetail,
)


def test_api_response():
    resp = ApiResponse(result={"id": 1, "name": "test"})
    data = msgspec.json.encode(resp)
    decoded = msgspec.json.decode(data, type=ApiResponse)
    assert decoded.result == {"id": 1, "name": "test"}


def test_api_list_response():
    resp = ApiListResponse(result=[{"id": 1}, {"id": 2}], count=2)
    data = msgspec.json.encode(resp)
    decoded = msgspec.json.decode(data, type=ApiListResponse)
    assert decoded.count == 2
    assert len(decoded.result) == 2


def test_error_response_sip40():
    detail = SupersetErrorDetail(message="Not found", error_type="NOT_FOUND")
    resp = ErrorResponse(errors=[detail], message="Not found")
    data = msgspec.json.encode(resp)
    decoded = msgspec.json.decode(data, type=ErrorResponse)
    assert decoded.message == "Not found"
    assert len(decoded.errors) == 1
    assert decoded.errors[0].error_type == "NOT_FOUND"


def test_api_response_defaults():
    resp = ApiResponse()
    assert resp.result is None
    assert resp.message is None


def test_api_list_response_defaults():
    resp = ApiListResponse()
    assert resp.result == []
    assert resp.count == 0


def test_error_response_defaults():
    resp = ErrorResponse()
    assert resp.errors == []
    assert resp.message == ""
