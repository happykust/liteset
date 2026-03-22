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
"""Tests for AvailableDomainsController."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from liteset.controllers.available_domains import AvailableDomainsController


# Call the unbound method directly to avoid Controller.__init__ requiring an owner.
_get = AvailableDomainsController.get_available_domains.fn


@pytest.mark.asyncio
async def test_returns_domains_from_settings() -> None:
    """Controller returns the superset_webserver_domains from state settings."""
    state = MagicMock()
    state.settings.superset_webserver_domains = ["example.com", "test.com"]
    result = await _get(self=None, state=state)  # type: ignore[arg-type]
    assert result == {"result": ["example.com", "test.com"]}


@pytest.mark.asyncio
async def test_returns_empty_list_when_no_domains() -> None:
    """Returns empty list when domains not configured."""
    state = MagicMock()
    state.settings = MagicMock(spec=[])  # no superset_webserver_domains attr
    result = await _get(self=None, state=state)  # type: ignore[arg-type]
    assert result == {"result": []}


def test_controller_path() -> None:
    assert AvailableDomainsController.path == "/api/v1/available_domains"
