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
from litestar import Litestar, get
from litestar.testing import AsyncTestClient

from liteset.exceptions import (
    CommandException,
    CommandInvalidError,
    CreateFailedError,
    DeleteFailedError,
    ForbiddenError,
    ImportFailedError,
    LitesetException,
    LitesetNotFoundError,
    LitesetSecurityException,
    LitesetTimeoutException,
    LitesetValidationException,
    ObjectNotFoundError,
    UpdateFailedError,
    liteset_exception_handler,
)


def test_liteset_exception_defaults():
    exc = LitesetException()
    assert exc.status_code == 500
    assert exc.message == "An unexpected error occurred"


def test_liteset_exception_custom_message():
    exc = LitesetException(message="boom")
    sip40 = exc.to_sip40()
    assert sip40["message"] == "boom"
    assert sip40["error_type"] == "LitesetException"
    assert sip40["level"] == "error"
    assert sip40["extra"] == {}


def test_security_exception():
    exc = LitesetSecurityException("nope")
    assert exc.status_code == 403


def test_validation_exception_with_extra():
    exc = LitesetValidationException("bad", extra={"field": "name"})
    sip40 = exc.to_sip40()
    assert sip40["extra"] == {"field": "name"}
    assert exc.status_code == 422


def test_not_found():
    exc = LitesetNotFoundError("gone")
    assert exc.status_code == 404


def test_timeout():
    exc = LitesetTimeoutException()
    assert exc.status_code == 504


def test_object_not_found_message():
    exc = ObjectNotFoundError("Chart", 42)
    assert exc.message == 'Chart "42" not found.'
    assert exc.status_code == 404


def test_object_not_found_no_id():
    exc = ObjectNotFoundError("Dashboard")
    assert exc.message == "Dashboard not found."


def test_command_invalid():
    exc = CommandInvalidError("bad data")
    assert exc.status_code == 422


def test_command_exception_with_nested():
    inner = ValueError("inner")
    exc = CommandException("outer", exceptions=[inner])
    assert exc.exceptions == [inner]


def test_create_update_delete_failed():
    assert CreateFailedError().status_code == 500
    assert UpdateFailedError().status_code == 500
    assert DeleteFailedError().status_code == 500
    assert ImportFailedError().status_code == 500


def test_forbidden():
    assert ForbiddenError().status_code == 403


async def test_handler_returns_sip40_json():
    @get("/fail")
    async def fail_route() -> None:
        raise LitesetSecurityException("access denied")

    app = Litestar(
        route_handlers=[fail_route],
        exception_handlers={LitesetException: liteset_exception_handler},
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/fail")
        assert resp.status_code == 403
        data = resp.json()
        assert "errors" in data
        assert len(data["errors"]) == 1
        assert data["errors"][0]["message"] == "access denied"
        assert data["errors"][0]["level"] == "error"
        assert "message" in data  # legacy compat field
