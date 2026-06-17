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
"""Tests for the event-logger dynamic binding in superset/tasks/sql_lab.py.

Regression test for the finding that ``from superset.events import event_logger``
created a module-level stale binding at import time so any subsequent call to
``configure_event_logger`` was invisible to the sql_lab module.

The original Apache Superset used a ``LocalProxy`` that dynamically resolved
the event logger at every access.  The liteset fix replaces the stale
``from X import Y`` binding with a module reference (``import superset.events
as _superset_events``) so ``_superset_events.event_logger`` always returns the
currently-registered singleton.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_query(database=None):
    """Build a minimal query-like object for _execute_query."""
    db = database or MagicMock()
    db.db_engine_spec = MagicMock()
    db.db_engine_spec.extract_error_message = lambda ex: str(ex)
    db.db_engine_spec.fetch_data = None  # use fallback
    q = SimpleNamespace(
        id=1,
        executed_sql="SELECT 1",
        limit=None,
        status="running",
        database=db,
    )
    return q


# ---------------------------------------------------------------------------
# Finding: stale binding — event_logger is resolved at call time, not import
# ---------------------------------------------------------------------------


def test_sql_lab_does_not_bind_event_logger_at_import():
    """sql_lab must NOT have a module-level ``event_logger`` name.

    Before the fix, ``from superset.events import event_logger`` captured the
    ``_StructuredLoggerLogger()`` singleton at import time.  After the fix, the
    module uses ``import superset.events as _superset_events`` and accesses
    ``_superset_events.event_logger`` at call time.
    """
    import superset.tasks.sql_lab as sql_lab_mod

    # The name ``event_logger`` must NOT be a direct attribute of the module.
    # If it is, the stale binding is back.
    assert not hasattr(sql_lab_mod, "event_logger"), (
        "sql_lab.py must not bind 'event_logger' at module level; "
        "it should access superset.events.event_logger dynamically."
    )

    # The bridging module reference must be present instead.
    assert hasattr(sql_lab_mod, "_superset_events"), (
        "sql_lab.py must import superset.events as _superset_events "
        "so event_logger is resolved at call time."
    )


def test_event_logger_binding_is_dynamic(monkeypatch):
    """Replacing superset.events.event_logger is immediately visible to sql_lab.

    This is the original contract from the Flask LocalProxy: any call site that
    accesses ``event_logger`` after ``configure_event_logger()`` sees the new
    instance.  The old stale ``from superset.events import event_logger`` broke
    this because Python's ``from X import Y`` never follows re-bindings of
    ``X.Y``.
    """
    import superset.events as events_mod
    import superset.tasks.sql_lab as sql_lab_mod

    sentinel = MagicMock()
    monkeypatch.setattr(events_mod, "event_logger", sentinel)

    # sql_lab must now see the sentinel through its module reference.
    assert sql_lab_mod._superset_events.event_logger is sentinel


def test_configure_event_logger_is_seen_by_sql_lab(monkeypatch):
    """After configure_event_logger() the sql_lab module uses the new logger.

    ``init_app()`` replaced the event logger and Celery tasks running inside
    ``app.test_request_context()`` resolved to the new ``DBEventLogger``
    via the ``LocalProxy``.
    """
    import superset.events as events_mod
    import superset.tasks.sql_lab as sql_lab_mod

    replacement = MagicMock()
    replacement.__class__.__name__ = "MockEventLogger"
    monkeypatch.setattr(events_mod, "event_logger", replacement)

    # After monkeypatch, the sql_lab module must see the replacement.
    seen = sql_lab_mod._superset_events.event_logger
    assert seen is replacement, (
        "sql_lab should see the replacement logger after re-binding; "
        "stale binding would return the original _StructuredLoggerLogger."
    )


# ---------------------------------------------------------------------------
# _execute_query uses the current event_logger (not a stale copy)
# ---------------------------------------------------------------------------


def test_execute_query_uses_current_event_logger(monkeypatch):
    """_execute_query wraps execution in event_logger.log_context.

    The context manager must be obtained from ``superset.events.event_logger``
    at call time so that replacing the singleton (e.g. via
    ``configure_event_logger``) is immediately effective.
    """
    import superset.events as events_mod
    import superset.tasks.sql_lab as sql_lab_mod

    logged_actions: list[str] = []

    # Build a fake event logger whose log_context records the action.
    class _RecordingLogger:
        @contextmanager
        def log_context(self, action, **_kwargs):
            logged_actions.append(action)
            yield lambda **kw: None

    recording = _RecordingLogger()
    monkeypatch.setattr(events_mod, "event_logger", recording)

    # Build a minimal query + cursor for _execute_query.
    db_spec = MagicMock()
    db_spec.execute_with_cursor = MagicMock()
    db_spec.extract_error_message = lambda ex: str(ex)
    db_spec.fetch_data = MagicMock(return_value=[])

    db = MagicMock()
    db.db_engine_spec = db_spec

    query = SimpleNamespace(
        id=1,
        executed_sql="SELECT 1",
        limit=None,
        status="running",
        database=db,
    )
    cursor = MagicMock()
    cursor.description = None
    session = MagicMock()

    with (
        patch("superset.tasks.sql_lab._resolve_query_logger", return_value=None),
        patch(
            "superset.tasks.sql_lab._wrap_result_set",
            return_value=MagicMock(columns=[], size=0),
        ),
    ):
        sql_lab_mod._execute_query(session, query, cursor, db_spec)

    assert logged_actions == ["execute_sql"], (
        "_execute_query must log the 'execute_sql' action via the current "
        "event_logger (recording saw: %r)" % logged_actions
    )


# ---------------------------------------------------------------------------
# Worker init: configure_event_logger is called in init_worker_db_engine
# ---------------------------------------------------------------------------


def test_init_worker_db_engine_configures_event_logger(monkeypatch):
    """init_worker_db_engine calls configure_event_logger with a session factory.

    In the original Flask app, ``init_app()`` configured ``DBEventLogger``
    before any Celery task ran.  In the liteset port, ``on_startup`` calls
    ``configure_event_logger`` for the web process, but Celery workers never
    run ``on_startup``.  ``init_worker_db_engine`` must therefore call
    ``configure_event_logger`` itself so that SQL-Lab async queries write
    ``Log`` rows.
    """
    import superset.tasks.celery_app as celery_mod

    fake_engine = MagicMock(name="fake_engine")
    fake_factory = MagicMock(name="fake_factory")
    configured_factories: list[object] = []

    def fake_create_worker_engine(url, **kw):
        return fake_engine

    def fake_create_session_factory(eng):
        assert eng is fake_engine
        return fake_factory

    def fake_configure_event_logger(session_factory=None):
        configured_factories.append(session_factory)

    fake_settings = MagicMock()
    fake_settings.sqlalchemy_database_uri = "sqlite+aiosqlite:///x.db"
    fake_settings.feature_flags = {}
    fake_settings.stats_logger = MagicMock()

    with (
        # All imports inside init_worker_db_engine are resolved through
        # their canonical module paths (not through the celery_app namespace).
        patch("superset.config.SupersetSettings", return_value=fake_settings),
        patch(
            "superset.db.session.create_worker_engine",
            side_effect=fake_create_worker_engine,
        ),
        patch(
            "superset.db.session.get_engine",
            side_effect=RuntimeError("no engine"),
        ),
        patch("superset.db.session.dispose_engine"),
        patch(
            "superset.db.session.create_session_factory",
            side_effect=fake_create_session_factory,
        ),
        patch(
            "superset.events.configure_event_logger",
            side_effect=fake_configure_event_logger,
        ),
        patch("superset.extensions.stats_logger_manager"),
        patch("superset.utils.feature_flags.feature_flag_manager"),
    ):
        celery_mod.init_worker_db_engine()

    assert configured_factories == [fake_factory], (
        "init_worker_db_engine must call configure_event_logger(session_factory=...) "
        "so that Celery workers write audit Log rows.  Got: %r" % configured_factories
    )
