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
"""Tests for the OAuth2 dance branch in the sync ``BaseEngineSpec.execute``.

1:1 with ``superset_old/db_engine_specs/base.py::execute`` — on a DB error,
if OAuth2 is enabled for the database and the error indicates that
authorization is required, the OAuth2 dance is started (raising
``OAuth2RedirectError``) *before* the exception is mapped.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from superset.db_engine_specs.base import BaseEngineSpec
from superset.exceptions import OAuth2RedirectError


class _OAuth2Required(Exception):
    """Sentinel error standing in for an engine's "auth needed" exception."""


class _SpecWithOAuth2(BaseEngineSpec):
    """Sync engine spec whose ``oauth2_exception`` matches ``_OAuth2Required``."""

    engine = "sqlite"
    oauth2_exception = _OAuth2Required


def _cursor_raising(exc: Exception) -> MagicMock:
    cursor = MagicMock()
    cursor.execute.side_effect = exc
    return cursor


def test_execute_oauth2_enabled_and_needed_starts_dance() -> None:
    """oauth2-enabled DB + oauth2 error -> OAuth2RedirectError (dance started)."""
    database = MagicMock()
    database.is_oauth2_enabled.return_value = True

    started: dict[str, object] = {}

    async def _fake_dance(db: object) -> None:
        started["db"] = db
        raise OAuth2RedirectError("https://auth.example/authorize", "tab-1", None)

    _SpecWithOAuth2.start_oauth2_dance = classmethod(  # type: ignore[assignment]
        lambda cls, db: _fake_dance(db)
    )

    with pytest.raises(OAuth2RedirectError):
        _SpecWithOAuth2.execute(
            _cursor_raising(_OAuth2Required("token expired")),
            "SELECT 1",
            database,
        )

    # The dance was actually invoked with our database.
    assert started["db"] is database


def test_execute_oauth2_disabled_does_not_start_dance() -> None:
    """oauth2 *disabled* DB -> just the mapped exception, no dance."""
    database = MagicMock()
    database.is_oauth2_enabled.return_value = False

    called = {"dance": False}

    async def _fake_dance(db: object) -> None:
        called["dance"] = True
        raise OAuth2RedirectError("https://auth.example/authorize", "tab-1", None)

    _SpecWithOAuth2.start_oauth2_dance = classmethod(  # type: ignore[assignment]
        lambda cls, db: _fake_dance(db)
    )

    # Not an OAuth2RedirectError — the dance never runs, so the original
    # error is mapped/re-raised instead.
    with pytest.raises(Exception) as exc_info:
        _SpecWithOAuth2.execute(
            _cursor_raising(_OAuth2Required("token expired")),
            "SELECT 1",
            database,
        )

    assert not isinstance(exc_info.value, OAuth2RedirectError)
    assert called["dance"] is False


def test_execute_non_oauth2_error_does_not_start_dance() -> None:
    """oauth2-enabled DB but a *non*-oauth2 error -> mapped, no dance."""
    database = MagicMock()
    database.is_oauth2_enabled.return_value = True

    called = {"dance": False}

    async def _fake_dance(db: object) -> None:
        called["dance"] = True
        raise OAuth2RedirectError("https://auth.example/authorize", "tab-1", None)

    _SpecWithOAuth2.start_oauth2_dance = classmethod(  # type: ignore[assignment]
        lambda cls, db: _fake_dance(db)
    )

    with pytest.raises(Exception) as exc_info:
        _SpecWithOAuth2.execute(
            _cursor_raising(ValueError("syntax error")),
            "SELECT 1",
            database,
        )

    assert not isinstance(exc_info.value, OAuth2RedirectError)
    assert called["dance"] is False
