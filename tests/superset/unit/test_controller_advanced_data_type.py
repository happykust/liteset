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
"""Tests for AdvancedDataTypeController."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from superset.controllers.advanced_data_type import AdvancedDataTypeController
from superset.exceptions import SupersetValidationException
from superset.schemas.advanced_data_type import AdvancedDataTypeConvertRequest

# Extract unbound methods to avoid Controller.__init__ requiring an owner.
_get_types = AdvancedDataTypeController.get_types.fn
_convert = AdvancedDataTypeController.convert.fn
_convert_get = AdvancedDataTypeController.convert_get.fn


def _make_state_with_registry() -> MagicMock:
    state = MagicMock()
    state.settings.advanced_data_types = {
        "internet_address": lambda vals: [
            {"value": v, "type": "internet_address"} for v in vals
        ],
    }
    return state


@pytest.mark.asyncio
async def test_get_types() -> None:
    state = _make_state_with_registry()
    result = await _get_types(self=None, state=state)  # type: ignore[arg-type]
    # Returns exactly what is in settings.advanced_data_types — no built-in
    # injection in the controller (mirrors original app.config read).
    assert result == {"result": ["internet_address"]}


@pytest.mark.asyncio
async def test_get_types_empty() -> None:
    state = MagicMock()
    state.settings = MagicMock(spec=[])
    result = await _get_types(self=None, state=state)  # type: ignore[arg-type]
    # No settings attribute → getattr fallback is {} → empty list.
    assert result == {"result": []}


@pytest.mark.asyncio
async def test_get_types_respects_user_empty_registry() -> None:
    """ADVANCED_DATA_TYPES={} must produce an empty /types list.

    In the original, ``app.config['ADVANCED_DATA_TYPES']`` is read
    directly (superset_old/advanced_data_type/api.py:148).  If a user
    sets ``ADVANCED_DATA_TYPES = {}`` the result is ``[]``.  Liteset
    must not inject built-in types unconditionally.
    """
    state = MagicMock()
    state.settings.advanced_data_types = {}
    result = await _get_types(self=None, state=state)  # type: ignore[arg-type]
    assert result == {"result": []}


@pytest.mark.asyncio
async def test_get_types_custom_registry_only() -> None:
    """Custom ADVANCED_DATA_TYPES without built-ins returns only custom keys.

    Mirrors original behaviour: if a user provides
    ``ADVANCED_DATA_TYPES = {'my_type': ...}`` the built-in
    ``internet_address`` and ``port`` do NOT appear in the response.
    """
    state = MagicMock()
    state.settings.advanced_data_types = {"my_type": MagicMock()}
    result = await _get_types(self=None, state=state)  # type: ignore[arg-type]
    assert result == {"result": ["my_type"]}


@pytest.mark.asyncio
async def test_convert_success() -> None:
    state = _make_state_with_registry()
    request_data = AdvancedDataTypeConvertRequest(
        type="internet_address",
        values=["192.168.1.1"],
    )
    result = await _convert(self=None, data=request_data, state=state)  # type: ignore[arg-type]
    assert result == {"result": [{"value": "192.168.1.1", "type": "internet_address"}]}


@pytest.mark.asyncio
async def test_convert_unknown_type() -> None:
    """Unknown type returns HTTP 400 "Invalid advanced data type: <type>".

    Mirrors superset_old/advanced_data_type/api.py ``get`` which returns
    ``self.response(400, message="Invalid advanced data type: ...")``.
    """
    state = _make_state_with_registry()
    request_data = AdvancedDataTypeConvertRequest(
        type="unknown_type",
        values=["test"],
    )
    response = await _convert(self=None, data=request_data, state=state)  # type: ignore[arg-type]
    assert response.status_code == 400
    assert response.content == {"message": "Invalid advanced data type: unknown_type"}


@pytest.mark.asyncio
async def test_convert_fetch_data_handler() -> None:
    """Handler with fetch_data method instead of being directly callable."""

    class _Handler:
        """Non-callable handler that exposes fetch_data."""

        def fetch_data(self, values: list[str]) -> list[dict[str, str]]:
            return [{"value": v, "type": "cidr"} for v in values]

    state = MagicMock()
    state.settings.advanced_data_types = {"cidr": _Handler()}

    request_data = AdvancedDataTypeConvertRequest(
        type="cidr",
        values=["10.0.0.1"],
    )
    result = await _convert(self=None, data=request_data, state=state)  # type: ignore[arg-type]
    assert result == {"result": [{"value": "10.0.0.1", "type": "cidr"}]}


@pytest.mark.asyncio
async def test_convert_not_callable_handler() -> None:
    """Handler that is neither callable nor has fetch_data."""
    state = MagicMock()
    state.settings.advanced_data_types = {"bad": "not_a_handler"}

    request_data = AdvancedDataTypeConvertRequest(
        type="bad",
        values=["x"],
    )
    with pytest.raises(SupersetValidationException, match="not callable"):
        await _convert(self=None, data=request_data, state=state)  # type: ignore[arg-type]


def test_controller_path() -> None:
    assert AdvancedDataTypeController.path == "/api/v1/advanced_data_type"


# ---------------------------------------------------------------------------
# convert_get (GET /convert) — RISON parameter validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_convert_get_list_rison_returns_400() -> None:
    """GET /convert with a list RISON value must return HTTP 400, not 500.

    The original @rison(advanced_data_type_convert_schema) validates the RISON
    parameter against {"type": "object"}.  A list (e.g. ?q=!(1,2,3)) fails
    JSON-schema validation and FAB returns 400
    ("Not a valid rison schema [...]").  In liteset, provide_rison_query
    accepts both dicts and lists; without an explicit type guard the handler
    would call list.get("type") → AttributeError → HTTP 500.
    """
    state = _make_state_with_registry()
    response = await _convert_get(
        self=None,
        rison_params=[1, 2, 3],
        state=state,  # type: ignore[arg-type]
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_convert_get_missing_type_returns_400() -> None:
    """RISON dict missing 'type' key → 400 (mirrors original schema required)."""
    state = _make_state_with_registry()
    response = await _convert_get(
        self=None,
        rison_params={"values": ["192.168.1.1"]},  # type is missing
        state=state,
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_convert_get_missing_values_returns_400() -> None:
    """RISON dict missing 'values' key → 400 (mirrors original schema required)."""
    state = _make_state_with_registry()
    response = await _convert_get(
        self=None,
        rison_params={"type": "internet_address"},  # values is missing
        state=state,
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_convert_get_none_rison_returns_400() -> None:
    """No 'q' parameter at all (rison_params=None) → 400 for missing fields."""
    state = _make_state_with_registry()
    response = await _convert_get(
        self=None,
        rison_params=None,
        state=state,
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_convert_get_success() -> None:
    """Valid RISON dict with type and values → 200 with result."""
    state = _make_state_with_registry()
    result = await _convert_get(
        self=None,
        rison_params={"type": "internet_address", "values": ["10.0.0.1"]},
        state=state,
    )
    # Returns a plain dict (not Response) on success
    assert isinstance(result, dict)
    assert "result" in result


@pytest.mark.asyncio
async def test_convert_get_unknown_type_returns_400() -> None:
    """Valid RISON but unknown type → 400 "Invalid advanced data type"."""
    state = _make_state_with_registry()
    response = await _convert_get(
        self=None,
        rison_params={"type": "no_such_type", "values": ["x"]},
        state=state,
    )
    assert response.status_code == 400
