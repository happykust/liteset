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
"""Unit tests for TableSchemaController.set_expanded parity with original.

Regression guard: the original TableSchemaView.expanded
(superset_old/views/sql_lab/views.py:270-278) has NO try/except, so
a missing 'expanded' form key (KeyError) or invalid JSON (JSONDecodeError)
propagates as an unhandled exception — Flask/Werkzeug returns HTTP 500.

A prior liteset port wrapped the body in try/except and returned HTTP 400,
which is a client-observable status-code regression. The fix removes the
try/except so exceptions propagate identically to the original.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.controllers.tab_state import TableSchemaController


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.set_expanded = AsyncMock()
    return dao


def _make_request(
    form_data: dict | None = None, missing_key: bool = False
) -> MagicMock:
    """Return a mock Litestar Request whose form() returns form_data."""
    request = MagicMock()
    if missing_key:
        # ImmutableMultiDict-like: KeyError on missing key
        form_dict: dict = {}
    else:
        form_dict = form_data or {}

    async def mock_form():
        return form_dict

    request.form = mock_form
    return request


# ---------------------------------------------------------------------------
# Happy path — matches json_success(response) in original
# ---------------------------------------------------------------------------


async def test_set_expanded_true_returns_200_body(mock_dao):
    """Valid expanded=true returns {id, expanded: true} with HTTP 200.

    Original: json_success(json.dumps({"id": table_schema_id, "expanded": payload}))
    (superset_old/views/sql_lab/views.py:277-278).
    """
    handler = TableSchemaController.set_expanded
    fn = handler.fn if hasattr(handler, "fn") else handler

    request = _make_request({"expanded": "true"})
    resp = await fn(
        TableSchemaController(owner=MagicMock()),
        table_schema_id=7,
        request=request,
        dao=mock_dao,
    )

    body = json.loads(resp.content)
    assert body == {"id": 7, "expanded": True}
    mock_dao.set_expanded.assert_awaited_once_with(7, True)


async def test_set_expanded_false_returns_200_body(mock_dao):
    """Valid expanded=false returns {id, expanded: false}."""
    handler = TableSchemaController.set_expanded
    fn = handler.fn if hasattr(handler, "fn") else handler

    request = _make_request({"expanded": "false"})
    resp = await fn(
        TableSchemaController(owner=MagicMock()),
        table_schema_id=3,
        request=request,
        dao=mock_dao,
    )

    body = json.loads(resp.content)
    assert body == {"id": 3, "expanded": False}
    mock_dao.set_expanded.assert_awaited_once_with(3, False)


# ---------------------------------------------------------------------------
# Error path — no try/except; exceptions propagate (original: HTTP 500)
# ---------------------------------------------------------------------------


async def test_set_expanded_missing_key_propagates_key_error(mock_dao):
    """Missing 'expanded' form field → HTTP 400 (Werkzeug BadRequestKeyError).

    Original: ``request.form["expanded"]`` raises Werkzeug's
    ``BadRequestKeyError`` — a ``BadRequest`` (HTTP 400) subclass that Flask
    renders as a 400 response, NOT a plain KeyError/500.
    """
    from litestar.exceptions import ClientException

    handler = TableSchemaController.set_expanded
    fn = handler.fn if hasattr(handler, "fn") else handler

    request = _make_request(missing_key=True)

    with pytest.raises(ClientException):
        await fn(
            TableSchemaController(owner=MagicMock()),
            table_schema_id=5,
            request=request,
            dao=mock_dao,
        )

    # DAO must not have been called
    mock_dao.set_expanded.assert_not_awaited()


async def test_set_expanded_invalid_json_propagates_decode_error(mock_dao):
    """Invalid JSON for 'expanded' raises JSONDecodeError (not caught → 500).

    Original: json.loads(request.form["expanded"]) raises json.JSONDecodeError
    when the value is not valid JSON; Flask/Werkzeug returns HTTP 500.
    Regression guard: a prior liteset port caught this and returned 400.
    """
    handler = TableSchemaController.set_expanded
    fn = handler.fn if hasattr(handler, "fn") else handler

    request = _make_request({"expanded": "not-valid-json!!!"})

    with pytest.raises(json.JSONDecodeError):
        await fn(
            TableSchemaController(owner=MagicMock()),
            table_schema_id=5,
            request=request,
            dao=mock_dao,
        )

    mock_dao.set_expanded.assert_not_awaited()


async def test_set_expanded_dao_error_propagates(mock_dao):
    """DAO-level exception propagates without being caught (original: 500).

    The original has no try/except; a database error propagates to Flask → 500.
    The liteset must do the same: no silent swallow into a 400 response.
    """
    handler = TableSchemaController.set_expanded
    fn = handler.fn if hasattr(handler, "fn") else handler

    mock_dao.set_expanded.side_effect = RuntimeError("db boom")
    request = _make_request({"expanded": "true"})

    with pytest.raises(RuntimeError, match="db boom"):
        await fn(
            TableSchemaController(owner=MagicMock()),
            table_schema_id=9,
            request=request,
            dao=mock_dao,
        )
