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
"""Unit tests for the guest-user RLS path in _sync_get_rls_rules.

Expected behaviour:
  - When EMBEDDED_SUPERSET is enabled and the current user is a guest user,
    ``current_user_rls_rules`` must return sorted clauses taken from the guest
    token's ``rls`` list, filtered to rules whose ``dataset`` field matches the
    table's id (or rules with no ``dataset`` key, which apply globally).
  - When EMBEDDED_SUPERSET is disabled, the guest path is NOT taken even if
    the user object carries ``is_guest=True``.

In the old liteset code (before the fix) the inline guest check called
``AsyncSecurityManager.is_guest_user(None, user)`` with ``None`` as ``self``;
that raised ``AttributeError`` which was swallowed, so ``is_guest`` was always
``False`` and guest users always fell through to the DB path where they had no
roles, so they always received zero RLS clauses.  These tests pin the FIXED
behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from superset.jinja_context import _sync_get_rls_rules, ExtraCache


def _make_table(table_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(id=table_id)


def _make_guest_user(rls_rules: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(is_guest=True, roles=[], rls_rules=rls_rules or [])


def _make_regular_user(role_ids: list[int] | None = None) -> SimpleNamespace:
    roles = [SimpleNamespace(id=rid) for rid in (role_ids or [])]
    return SimpleNamespace(is_guest=False, roles=roles, rls_rules=[])


def _ff_enabled(feature: str) -> bool:
    return True


def _ff_disabled(feature: str) -> bool:
    return False


def test_guest_rls_returns_dataset_scoped_clauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guest user with EMBEDDED_SUPERSET=True gets RLS from token, not DB.

    Rules whose ``dataset`` field matches table.id are included; rules for a
    different dataset are excluded; rules with no ``dataset`` key are included
    globally.
    """
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=_ff_enabled),
    )
    table = _make_table(42)
    user = _make_guest_user(
        [
            {"clause": "team_id=1", "dataset": 42},  # matches → included
            {"clause": "org_id=5", "dataset": 99},  # wrong dataset → excluded
            {"clause": "global_filter=1"},  # no dataset → always included
        ]
    )
    result = _sync_get_rls_rules(table, user)
    assert result == sorted(["team_id=1", "global_filter=1"])


def test_guest_rls_returns_empty_when_no_matching_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=_ff_enabled),
    )
    table = _make_table(42)
    user = _make_guest_user([{"clause": "team_id=1", "dataset": 99}])
    assert _sync_get_rls_rules(table, user) == []


def test_guest_rls_all_dataset_rules_when_no_dataset_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rules with no ``dataset`` key apply to every dataset."""
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=_ff_enabled),
    )
    table = _make_table(1)
    user = _make_guest_user(
        [
            {"clause": "a"},
            {"clause": "b"},
        ]
    )
    assert _sync_get_rls_rules(table, user) == ["a", "b"]


def test_guest_rls_not_taken_when_embedded_superset_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When EMBEDDED_SUPERSET=False, the guest path is never entered.

    The DB path returns [] immediately for users with no role_ids.
    The rls_rules on the user object must NOT be read.
    """
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=_ff_disabled),
    )
    table = _make_table(42)
    user = _make_guest_user([{"clause": "should_not_appear", "dataset": 42}])
    # No role_ids → DB path short-circuits to []
    result = _sync_get_rls_rules(table, user)
    assert result == []


def test_guest_rls_result_is_sorted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=_ff_enabled),
    )
    table = _make_table(5)
    user = _make_guest_user(
        [
            {"clause": "z_filter"},
            {"clause": "a_filter"},
            {"clause": "m_filter"},
        ]
    )
    result = _sync_get_rls_rules(table, user)
    assert result == ["a_filter", "m_filter", "z_filter"]


def test_guest_rls_rules_excludes_empty_clauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rules with an empty or missing clause value are silently skipped."""
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=_ff_enabled),
    )
    table = _make_table(42)
    user = _make_guest_user(
        [
            {"clause": ""},  # empty → excluded
            {"clause": None},  # type: ignore[dict-item]  # None → excluded
            {},  # missing key → excluded
            {"clause": "real_filter"},
        ]
    )
    result = _sync_get_rls_rules(table, user)
    assert result == ["real_filter"]


def test_extra_cache_current_user_rls_rules_guest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """current_user_rls_rules returns sorted guest-token clauses end-to-end.

    ``ExtraCache.current_user_rls_rules`` branches on the guest-user flag and
    applies dataset-scoped RLS rules from the token.
    """
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=_ff_enabled),
    )
    table = _make_table(10)
    user = _make_guest_user(
        [
            {"clause": "z_clause"},
            {"clause": "a_clause"},
            {"clause": "b_clause", "dataset": 99},  # excluded — wrong dataset
        ]
    )
    monkeypatch.setattr("superset.jinja_context.get_current_user", lambda: user)
    cache = ExtraCache(table=table)
    assert cache.current_user_rls_rules() == ["a_clause", "z_clause"]


def test_extra_cache_current_user_rls_rules_guest_no_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """current_user_rls_rules returns None when the guest has no matching rules."""
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=_ff_enabled),
    )
    table = _make_table(10)
    user = _make_guest_user([])
    monkeypatch.setattr("superset.jinja_context.get_current_user", lambda: user)
    cache = ExtraCache(table=table)
    # [] → falsy → current_user_rls_rules returns None (matches original)
    assert cache.current_user_rls_rules() is None


def test_extra_cache_current_user_rls_rules_guest_no_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """current_user_rls_rules returns None when no table is set."""
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=_ff_enabled),
    )
    user = _make_guest_user([{"clause": "test"}])
    monkeypatch.setattr("superset.jinja_context.get_current_user", lambda: user)
    cache = ExtraCache()  # no table
    assert cache.current_user_rls_rules() is None
