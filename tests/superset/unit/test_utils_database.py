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
"""Unit tests for superset/utils/database.py — 1:1 parity with upstream.

Covers:
- Finding 1: ENGINE_CONTEXT_MANAGER scope (all parameter prep inside CM)
- Finding 2: adjust_engine_params exceptions propagate (no swallowing)
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.engine import make_url


class _DB:
    sqlalchemy_uri = "sqlite:///test.db"
    sqlalchemy_uri_decrypted = sqlalchemy_uri
    impersonate_user = False

    # Use the base SQLite spec so adjust_engine_params is a real no-op call.
    from superset.db_engine_specs import get_engine_spec

    db_engine_spec = get_engine_spec("sqlite", "")

    def get_extra(self, source: Any = None) -> dict[str, Any]:
        return {}

    def get_effective_user(self, url: Any) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Finding 2: adjust_engine_params exception propagates (not swallowed)
# Regression: liteset had `try: ... except Exception: logger.debug(...)` around
# the adjust_engine_params call.  It is called bare — exceptions propagate.
# ---------------------------------------------------------------------------


def test_adjust_engine_params_exception_propagates(monkeypatch: Any) -> None:
    """adjust_engine_params raising must propagate out of _build_engine_kwargs_sync.

    The function calls adjust_engine_params with no surrounding try-except;
    exceptions propagate to the caller.
    Liteset regression: the call was wrapped in `except Exception: logger.debug`
    which silently swallowed the error and continued with the unmodified URI —
    the connection would then succeed against the wrong namespace or fail later
    with an opaque driver error instead of an informative early failure.
    """
    import superset.utils.database as ud

    spec = MagicMock()
    spec.adjust_engine_params.side_effect = ValueError("bad catalog")
    spec.validate_database_uri = MagicMock()

    class _ErrDB(_DB):
        db_engine_spec = spec

    with pytest.raises(ValueError, match="bad catalog"):
        ud._build_engine_kwargs_sync(
            _ErrDB(),
            "sqlite:///test.db",
            catalog="bad",
            schema=None,
            source=None,
            nullpool=False,
        )

    spec.adjust_engine_params.assert_called_once()


def test_adjust_engine_params_success_updates_uri(monkeypatch: Any) -> None:
    # Happy-path counterpart: the result of adjust_engine_params must be
    # applied to sync_uri, not ignored.
    import superset.utils.database as ud

    spec = MagicMock()
    new_url = make_url("sqlite:///adjusted.db")
    spec.adjust_engine_params.return_value = (new_url, {})
    spec.validate_database_uri = MagicMock()

    class _AdjDB(_DB):
        db_engine_spec = spec

    result_uri, _, _eff = ud._build_engine_kwargs_sync(
        _AdjDB(),
        "sqlite:///test.db",
        catalog=None,
        schema="myschema",
        source=None,
        nullpool=False,
    )

    spec.adjust_engine_params.assert_called_once()
    assert "adjusted" in result_uri


# ---------------------------------------------------------------------------
# Finding 1: ENGINE_CONTEXT_MANAGER scope — all parameter prep inside the CM
# Regression: liteset called _build_engine_kwargs_sync and
# _apply_connection_hooks OUTSIDE the CM, then only called create_engine
# inside it.  The function runs the entire _get_sqla_engine body (including
# adjust_engine_params, impersonation, update_params_from_encrypted_extra,
# and DB_CONNECTION_MUTATOR) inside the engine_context_manager.
# ---------------------------------------------------------------------------


def test_engine_context_manager_wraps_param_preparation(monkeypatch: Any) -> None:
    import superset.utils.database as ud

    call_log: list[str] = []

    # A recording CM: logs 'enter'/'exit' so we can assert that param-prep
    # happens between them.
    @contextmanager
    def _recording_cm(database: Any, catalog: Any, schema: Any):
        call_log.append("cm_enter")
        try:
            yield
        finally:
            call_log.append("cm_exit")

    real_build = ud._build_engine_kwargs_sync
    real_hooks = ud._apply_connection_hooks

    def _spy_build(*args: Any, **kwargs: Any) -> Any:
        call_log.append("build_kwargs")
        return real_build(*args, **kwargs)

    def _spy_hooks(*args: Any, **kwargs: Any) -> Any:
        call_log.append("apply_hooks")
        return real_hooks(*args, **kwargs)

    class _FakeEngine:
        url = make_url("sqlite:///test.db")

        def dispose(self) -> None:
            pass

        def connect(self) -> Any:
            return MagicMock().__enter__()

    def _fake_create_engine(uri: Any, **kw: Any) -> _FakeEngine:
        call_log.append("create_engine")
        return _FakeEngine()

    monkeypatch.setattr(ud, "_resolve_engine_context_manager", _recording_cm)
    monkeypatch.setattr(ud, "_build_engine_kwargs_sync", _spy_build)
    monkeypatch.setattr(ud, "_apply_connection_hooks", _spy_hooks)
    monkeypatch.setattr(ud, "create_engine", _fake_create_engine)

    with ud.get_sync_engine(_DB()):
        pass

    assert "cm_enter" in call_log
    assert "build_kwargs" in call_log
    assert "apply_hooks" in call_log
    assert "create_engine" in call_log
    assert "cm_exit" in call_log

    cm_enter_idx = call_log.index("cm_enter")
    cm_exit_idx = call_log.index("cm_exit")
    build_idx = call_log.index("build_kwargs")
    hooks_idx = call_log.index("apply_hooks")
    create_idx = call_log.index("create_engine")

    assert cm_enter_idx < build_idx, "build_kwargs must run AFTER CM enters"
    assert cm_enter_idx < hooks_idx, "apply_hooks must run AFTER CM enters"
    assert cm_enter_idx < create_idx, "create_engine must run AFTER CM enters"
    assert build_idx < cm_exit_idx, "build_kwargs must run BEFORE CM exits"
    assert hooks_idx < cm_exit_idx, "apply_hooks must run BEFORE CM exits"
    assert create_idx < cm_exit_idx, "create_engine must run BEFORE CM exits"


def test_engine_context_manager_exception_in_param_prep_propagates(
    monkeypatch: Any,
) -> None:
    """Exceptions raised by param-prep inside the CM propagate to the caller.

    When _build_engine_kwargs_sync raises (e.g. adjust_engine_params error),
    the CM still exits cleanly (its __exit__ is called) and the exception
    propagates to the get_sync_engine caller.
    """
    import superset.utils.database as ud

    cm_exited = {"value": False}

    @contextmanager
    def _recording_cm(database: Any, catalog: Any, schema: Any):
        try:
            yield
        finally:
            cm_exited["value"] = True

    def _failing_build(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("adjust_engine_params failed")

    monkeypatch.setattr(ud, "_resolve_engine_context_manager", _recording_cm)
    monkeypatch.setattr(ud, "_build_engine_kwargs_sync", _failing_build)

    with pytest.raises(RuntimeError, match="adjust_engine_params failed"):
        with ud.get_sync_engine(_DB()):
            pass

    # The CM must have been exited (cleanup ran) even though param-prep raised.
    assert cm_exited["value"] is True


# ---------------------------------------------------------------------------
# Finding: effective_username for DB_CONNECTION_MUTATOR must be the pre-
# impersonation value.
#
# Regression: _apply_connection_hooks recomputed effective_username from the
# (post-impersonation) sqlalchemy_url it was passed, so DB_CONNECTION_MUTATOR
# received the impersonated name instead of the original effective_username.
#
# ---------------------------------------------------------------------------


def test_build_engine_kwargs_returns_pre_impersonation_effective_username() -> None:
    """_build_engine_kwargs_sync 3rd return value is pre-impersonation username.

    When impersonate_user rewrites the URL username, the 3rd returned value
    must be the username computed BEFORE the rewrite (from the post-adjust,
    pre-impersonation URL).
    """
    import superset.utils.database as ud

    original_url = make_url("postgresql+psycopg2://original_user:pass@host/db")
    impersonated_url = make_url("postgresql+psycopg2://impersonated_user:pass@host/db")

    spec = MagicMock()
    spec.adjust_engine_params.return_value = (original_url, {})
    spec.validate_database_uri = MagicMock()

    def _impersonate(
        database: Any, eff_user: Any, token: Any, url: Any, kw: Any
    ) -> tuple[Any, Any]:
        # Simulate impersonation rewriting the URL username.
        return impersonated_url, kw

    spec.impersonate_user = _impersonate

    class _ImpersonateDB(_DB):
        sqlalchemy_uri = "postgresql+psycopg2://original_user:pass@host/db"
        sqlalchemy_uri_decrypted = sqlalchemy_uri
        impersonate_user = True
        db_engine_spec = spec

        def get_effective_user(self, url: Any) -> str | None:
            # No logged-in user: fall back to url.username (original behavior).
            return url.username

    sync_uri, _kwargs, eff_user = ud._build_engine_kwargs_sync(
        _ImpersonateDB(),
        "postgresql+psycopg2://original_user:pass@host/db",
        catalog=None,
        schema=None,
        source=None,
        nullpool=False,
    )

    # After impersonation, sync_uri contains the impersonated username.
    assert "impersonated_user" in sync_uri

    # The 3rd element must be the PRE-impersonation username (from adjusted URL).
    assert eff_user == "original_user", (
        "effective_username for DB_CONNECTION_MUTATOR must be pre-impersonation"
    )


def test_db_connection_mutator_receives_pre_impersonation_username(
    monkeypatch: Any,
) -> None:
    """DB_CONNECTION_MUTATOR must receive the pre-impersonation effective_username.

    Regression: liteset recomputed effective_username from the post-impersonation
    URL inside _apply_connection_hooks.  When impersonate_user=True and no user
    is logged in (get_username() → None), get_effective_user falls back to
    url.username; after impersonation rewrites the URL the recomputed value
    diverges from the original.

    """
    import superset.utils.database as ud

    mutator_calls: list[str | None] = []

    def _mutator(
        url: Any,
        params: Any,
        eff_user: Any,
        sec_mgr: Any,
        src: Any,
    ) -> tuple[Any, Any]:
        mutator_calls.append(eff_user)
        return url, params

    class _FakeSettings:
        db_connection_mutator = staticmethod(_mutator)

    monkeypatch.setattr(
        "superset.config.SupersetSettings",
        lambda **kw: _FakeSettings(),
    )

    original_url = make_url("postgresql+psycopg2://original_user:pass@host/db")
    impersonated_url = make_url("postgresql+psycopg2://impersonated_user:pass@host/db")

    spec = MagicMock()
    spec.adjust_engine_params.return_value = (original_url, {})
    spec.validate_database_uri = MagicMock()

    def _impersonate(
        database: Any, eff_user: Any, token: Any, url: Any, kw: Any
    ) -> tuple[Any, Any]:
        return impersonated_url, kw

    spec.impersonate_user = _impersonate

    class _ImpersonateDB(_DB):
        sqlalchemy_uri = "postgresql+psycopg2://original_user:pass@host/db"
        sqlalchemy_uri_decrypted = sqlalchemy_uri
        impersonate_user = True
        db_engine_spec = spec

        def get_effective_user(self, url: Any) -> str | None:
            # No logged-in user: fall back to url.username (original behavior).
            return url.username

    sync_uri, engine_kwargs, eff_user = ud._build_engine_kwargs_sync(
        _ImpersonateDB(),
        "postgresql+psycopg2://original_user:pass@host/db",
        catalog=None,
        schema=None,
        source=None,
        nullpool=False,
    )

    ud._apply_connection_hooks(
        _ImpersonateDB(),
        make_url(sync_uri),  # post-impersonation URL
        engine_kwargs,
        None,
        eff_user,  # pre-impersonation value from _build_engine_kwargs_sync
    )

    assert len(mutator_calls) == 1
    assert mutator_calls[0] == "original_user", (
        "DB_CONNECTION_MUTATOR must receive pre-impersonation username "
        f"'original_user', got {mutator_calls[0]!r}"
    )


def test_legacy_test_helpers_importable():
    """The four legacy helpers used by tests/integration_tests must exist
    and reference real modules (the port once imported the phantom
    ``superset.db.base`` → ImportError at call time)."""
    import inspect

    from superset.utils.database import (
        get_example_database,
        get_main_database,
        get_or_create_db,
        remove_database,
    )

    for fn in (
        get_or_create_db,
        get_example_database,
        get_main_database,
        remove_database,
    ):
        assert callable(fn)
        src = inspect.getsource(fn)
        assert "superset.db.base import" not in src, (
            "phantom module superset.db.base referenced"
        )
