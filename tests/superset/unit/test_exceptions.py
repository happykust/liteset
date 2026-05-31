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
from litestar import get, Litestar
from litestar.testing import AsyncTestClient

from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CommandException,
    CommandInvalidError,
    CreateFailedError,
    DeleteFailedError,
    ForbiddenError,
    generic_exception_handler,
    ImportFailedError,
    ObjectNotFoundError,
    superset_exception_handler,
    SupersetException,
    SupersetNotFoundError,
    SupersetSecurityException,
    SupersetTimeoutException,
    SupersetValidationException,
    UpdateFailedError,
)


def test_superset_exception_defaults():
    exc = SupersetException()
    assert exc.status_code == 500
    assert exc.message == "An unexpected error occurred"


def test_superset_exception_custom_message():
    exc = SupersetException(message="boom")
    sip40 = exc.to_sip40()
    assert sip40["message"] == "boom"
    assert sip40["error_type"] == "SupersetException"
    assert sip40["level"] == "error"
    assert sip40["extra"] == {}


def test_security_exception():
    exc = SupersetSecurityException(
        SupersetError(
            message="nope",
            error_type=SupersetErrorType.GENERIC_BACKEND_ERROR,
            level=ErrorLevel.ERROR,
        )
    )
    assert exc.status_code == 403


def test_validation_exception_with_extra():
    exc = SupersetValidationException("bad", extra={"field": "name"})
    sip40 = exc.to_sip40()
    assert sip40["extra"] == {"field": "name"}
    assert exc.status_code == 422


def test_not_found():
    exc = SupersetNotFoundError("gone")
    assert exc.status_code == 404


def test_timeout():
    exc = SupersetTimeoutException(
        error_type=SupersetErrorType.BACKEND_TIMEOUT_ERROR,
        message="timed out",
    )
    assert exc.status_code == 408


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
        raise SupersetSecurityException(
            SupersetError(
                message="access denied",
                error_type=SupersetErrorType.GENERIC_BACKEND_ERROR,
                level=ErrorLevel.ERROR,
            )
        )

    app = Litestar(
        route_handlers=[fail_route],
        exception_handlers={SupersetException: superset_exception_handler},
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


async def test_generic_handler_returns_500_for_unknown():
    @get("/boom")
    async def boom_route() -> None:
        raise RuntimeError("unexpected")

    app = Litestar(
        route_handlers=[boom_route],
        exception_handlers={Exception: generic_exception_handler},
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/boom")
        assert resp.status_code == 500
        data = resp.json()
        assert data["errors"][0]["error_type"] == "UNKNOWN_ERROR"


async def test_generic_handler_preserves_http_404():
    @get("/missing")
    async def missing_route() -> None:
        from litestar.exceptions import NotFoundException

        raise NotFoundException(detail="not here")

    app = Litestar(
        route_handlers=[missing_route],
        exception_handlers={Exception: generic_exception_handler},
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/missing")
        assert resp.status_code == 404
        data = resp.json()
        assert data["errors"][0]["message"] == "not here"
