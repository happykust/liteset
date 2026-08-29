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
"""Tests for ``superset.utils.rls``: embedded/guest RLS gating and the
colon-escaping text-clause builder.

Two behaviours are pinned here:

* ``_embedded_superset_enabled`` must honour BOTH the settings field and the
  ``EMBEDDED_SUPERSET`` feature flag. Reading only the flag meant
  ``LITESET_EMBEDDED_SUPERSET=true`` with the flag at its default silently
  dropped every guest RLS clause.
* RLS clauses must keep colon escaping. Built via plain ``sqlalchemy.text()``
  a literal colon (e.g. ``role != ':admin'``) is parsed as an unbound bind
  parameter and rendered as ``NULL`` under ``literal_binds``, so the clause
  silently matches every row.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from superset.utils import rls as rls_module

# ---------------------------------------------------------------------------
# _embedded_superset_enabled / guest RLS clause inclusion
# ---------------------------------------------------------------------------


class _FakeSecurityManager:
    """Minimal stand-in exposing only what ``compose_rls_where_clauses``
    calls: async ``get_rls_filters`` and sync ``get_guest_rls_filters``."""

    def __init__(self, guest_rules: list[dict[str, object]]) -> None:
        self._guest_rules = guest_rules

    async def get_rls_filters(self, table, user=None):  # noqa: ARG002
        return []

    def get_guest_rls_filters(self, table, user=None):  # noqa: ARG002
        return self._guest_rules


def _table() -> SimpleNamespace:
    # No ``database`` attribute -> ``_rls_text_clause`` falls back to plain
    # ``text()``; irrelevant to what this section is testing.
    return SimpleNamespace(database=None)


async def test_guest_rls_included_when_only_settings_field_enabled():
    """``embedded_superset=True`` (settings field), feature flag left at its
    default False, must still produce the guest RLS clause."""
    # ``_embedded_superset_enabled`` checks the settings field FIRST and
    # returns immediately when it is True, without consulting the feature
    # flag manager at all — so no patching of the (default-False) flag is
    # needed here to prove this half of the reconciliation.
    settings = SimpleNamespace(embedded_superset=True)
    guest_rules = [{"clause": "tenant_id = 42"}]
    sec_mgr = _FakeSecurityManager(guest_rules)

    with patch.object(rls_module, "_cached_settings", return_value=settings):
        clauses = await rls_module.compose_rls_where_clauses(
            _table(), user=None, security_manager=sec_mgr
        )

    assert len(clauses) == 1
    compiled = str(clauses[0].compile(compile_kwargs={"literal_binds": True}))
    assert "tenant_id = 42" in compiled


async def test_guest_rls_included_when_only_feature_flag_enabled(monkeypatch):
    """``EMBEDDED_SUPERSET`` feature flag True, settings field left at its
    default False, must still produce the guest RLS clause — the OTHER half
    of the reconciliation."""
    settings = SimpleNamespace(embedded_superset=False)
    guest_rules = [{"clause": "tenant_id = 7"}]
    sec_mgr = _FakeSecurityManager(guest_rules)

    from superset.utils.feature_flags import feature_flag_manager

    monkeypatch.setattr(
        feature_flag_manager, "_feature_flags", {"EMBEDDED_SUPERSET": True}
    )

    with patch.object(rls_module, "_cached_settings", return_value=settings):
        clauses = await rls_module.compose_rls_where_clauses(
            _table(), user=None, security_manager=sec_mgr
        )

    assert len(clauses) == 1
    compiled = str(clauses[0].compile(compile_kwargs={"literal_binds": True}))
    assert "tenant_id = 7" in compiled


async def test_guest_rls_omitted_when_neither_form_enabled(monkeypatch):
    """Neither the settings field nor the feature flag set -> no guest
    clause (sanity check that the gate can still say no)."""
    settings = SimpleNamespace(embedded_superset=False)
    sec_mgr = _FakeSecurityManager([{"clause": "tenant_id = 1"}])

    from superset.utils.feature_flags import feature_flag_manager

    monkeypatch.setattr(feature_flag_manager, "_feature_flags", {})

    with patch.object(rls_module, "_cached_settings", return_value=settings):
        clauses = await rls_module.compose_rls_where_clauses(
            _table(), user=None, security_manager=sec_mgr
        )

    assert clauses == []


# ---------------------------------------------------------------------------
# _rls_text_clause — colon escaping
# ---------------------------------------------------------------------------


def test_rls_text_clause_escapes_colons_via_engine_spec():
    """A literal colon in an RLS clause must survive compilation, not be
    parsed as a bind parameter and rendered as NULL."""
    from superset.db_engine_specs.base import BaseEngineSpec

    table = SimpleNamespace(database=SimpleNamespace(db_engine_spec=BaseEngineSpec))
    clause = rls_module._rls_text_clause(table, "department != ':restricted'")

    compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert compiled == "(department != ':restricted')"
    assert "NULL" not in compiled


def test_rls_text_clause_escapes_colons_without_an_engine_spec():
    """A table with no resolvable engine spec must still escape colons.

    Falling back to a bare ``text()`` here would reproduce the very bug this
    helper exists to prevent: SQLAlchemy reads ``:restricted`` as an unbound
    bind parameter and renders it as the literal ``NULL`` under
    ``literal_binds``, so ``department != ':restricted'`` silently becomes
    ``department != 'NULL'`` and matches every row — with no error, while the
    rule still shows as active.
    """
    table = SimpleNamespace(database=None)
    clause = rls_module._rls_text_clause(table, "department != ':restricted'")

    compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert compiled == "(department != ':restricted')"
    assert "NULL" not in compiled


def test_rls_text_clause_no_colon_unaffected():
    """A clause with no colon compiles identically whether or not an
    engine spec is available."""
    from superset.db_engine_specs.base import BaseEngineSpec

    table_with_spec = SimpleNamespace(
        database=SimpleNamespace(db_engine_spec=BaseEngineSpec)
    )
    table_without_spec = SimpleNamespace(database=None)

    clause_a = rls_module._rls_text_clause(table_with_spec, "role = 'admin'")
    clause_b = rls_module._rls_text_clause(table_without_spec, "role = 'admin'")

    compiled_a = str(clause_a.compile(compile_kwargs={"literal_binds": True}))
    compiled_b = str(clause_b.compile(compile_kwargs={"literal_binds": True}))
    assert compiled_a == compiled_b == "(role = 'admin')"
