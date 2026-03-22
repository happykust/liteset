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

from liteset.controllers.advanced_data_type import AdvancedDataTypeController
from liteset.exceptions import LitesetValidationException
from liteset.schemas.advanced_data_type import AdvancedDataTypeConvertRequest

# Extract unbound methods to avoid Controller.__init__ requiring an owner.
_get_types = AdvancedDataTypeController.get_types.fn
_convert = AdvancedDataTypeController.convert.fn


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
    assert result == {"result": ["internet_address"]}


@pytest.mark.asyncio
async def test_get_types_empty() -> None:
    state = MagicMock()
    state.settings = MagicMock(spec=[])
    result = await _get_types(self=None, state=state)  # type: ignore[arg-type]
    assert result == {"result": []}


@pytest.mark.asyncio
async def test_convert_success() -> None:
    state = _make_state_with_registry()
    request_data = AdvancedDataTypeConvertRequest(
        type="internet_address",
        values=["192.168.1.1"],
    )
    result = await _convert(self=None, data=request_data, state=state)  # type: ignore[arg-type]
    assert result == {
        "result": [{"value": "192.168.1.1", "type": "internet_address"}]
    }


@pytest.mark.asyncio
async def test_convert_unknown_type() -> None:
    state = _make_state_with_registry()
    request_data = AdvancedDataTypeConvertRequest(
        type="unknown_type",
        values=["test"],
    )
    with pytest.raises(LitesetValidationException, match="Unknown"):
        await _convert(self=None, data=request_data, state=state)  # type: ignore[arg-type]


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
    with pytest.raises(LitesetValidationException, match="not callable"):
        await _convert(self=None, data=request_data, state=state)  # type: ignore[arg-type]


def test_controller_path() -> None:
    assert AdvancedDataTypeController.path == "/api/v1/advanced_data_type"
