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
"""Regression for the import-request parser used by the /import/ endpoints.

A missing file field must be a clean 4xx (matching upstream's
``if not upload: return self.response_400()``), NOT a 500. The old
``data: UploadFile = Body(MULTI_PART)`` injection crashed with
``StopIteration`` -> HTTP 500 when no file was uploaded.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from litestar.datastructures import UploadFile
from litestar.exceptions import ValidationException

from superset.controllers.base import parse_import_request
from superset.exceptions import CommandInvalidError


def _request(form: dict) -> MagicMock:
    req = MagicMock()
    req.form = AsyncMock(return_value=form)
    return req


def _upload(contents: bytes = b"zipbytes", filename: str = "x.zip") -> UploadFile:
    return UploadFile(
        content_type="application/zip", filename=filename, file_data=contents
    )


async def test_no_file_raises_4xx() -> None:
    """No UploadFile -> ValidationException, mapped to 400 (upstream parity)."""
    with pytest.raises(ValidationException, match="No file uploaded"):
        await parse_import_request(_request({"overwrite": "true"}))


async def test_empty_form_raises_4xx() -> None:
    with pytest.raises(ValidationException, match="No file uploaded"):
        await parse_import_request(_request({}))


async def test_extracts_file_and_options() -> None:
    form = {
        "formData": _upload(b"hello", "bundle.zip"),
        "overwrite": "true",
        "passwords": '{"databases/a.yaml": "pw"}',
    }
    buf, filename, overwrite, passwords, ssh_pw, ssh_pk, ssh_pkpw = (
        await parse_import_request(_request(form))
    )
    assert isinstance(buf, io.BytesIO)
    assert buf.getvalue() == b"hello"
    assert filename == "bundle.zip"
    assert overwrite is True
    assert passwords == {"databases/a.yaml": "pw"}
    assert ssh_pw == {}
    assert ssh_pk == {}
    assert ssh_pkpw == {}


async def test_overwrite_false_default() -> None:
    form = {"formData": _upload()}
    _, _, overwrite, *_ = await parse_import_request(_request(form))
    assert overwrite is False


async def test_invalid_json_option_raises_4xx() -> None:
    form = {"formData": _upload(), "passwords": "NOT JSON"}
    with pytest.raises(CommandInvalidError, match="Invalid JSON in 'passwords'"):
        await parse_import_request(_request(form))
