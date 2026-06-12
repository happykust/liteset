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
"""Parity tests for the implicit-tag SQLA event listener wiring.

1:1 with upstream ``register_sqla_event_listeners`` which is invoked ONLY when
``TAGGING_SYSTEM`` is enabled (superset_old/app.py:158-161) and registers the
``QueryUpdater`` on ``SavedQuery`` (user-saved queries), NOT ``Query`` (every
SQL Lab execution).
"""

from __future__ import annotations

from sqlalchemy import event

from superset.models import _listeners
from superset.models.slice import Slice
from superset.models.sql_lab import Query, SavedQuery


def test_tag_listeners_not_wired_when_flag_off() -> None:
    """Default test env has TAGGING_SYSTEM OFF → ``register()`` (already run at
    import) must NOT have wired any implicit-tag listeners. Upstream only calls
    ``register_sqla_event_listeners`` under the feature flag, so with tagging
    disabled a sync insert (Celery query worker, CLI import) creates no
    implicit ``owner:``/``type:`` tags."""
    assert not event.contains(
        Slice, "after_insert", _listeners._chart_tag_after_insert
    )
    assert not event.contains(
        Query, "after_insert", _listeners._query_tag_after_insert
    )
    assert not event.contains(
        SavedQuery, "after_insert", _listeners._query_tag_after_insert
    )


def test_query_tag_updater_targets_saved_query_not_query() -> None:
    """When TAGGING_SYSTEM is enabled the query tag updater must listen on
    ``SavedQuery`` (user-saved queries), 1:1 with upstream
    (superset_old/tags/core.py:51). Listening on ``Query`` would create an
    implicit ``type:query`` + ``owner:`` tag on EVERY SQL Lab execution."""
    _listeners._register_tag_listeners()
    try:
        assert event.contains(
            SavedQuery, "after_insert", _listeners._query_tag_after_insert
        )
        assert not event.contains(
            Query, "after_insert", _listeners._query_tag_after_insert
        )
    finally:
        # Clean up so the global registry is unaffected by this test.
        from superset.models.connectors import SqlaTable
        from superset.models.core import FavStar
        from superset.models.dashboard import Dashboard

        for target, ident, fn in (
            (Slice, "after_insert", _listeners._chart_tag_after_insert),
            (Slice, "after_update", _listeners._chart_tag_after_update),
            (Slice, "after_delete", _listeners._chart_tag_after_delete),
            (Dashboard, "after_insert", _listeners._dashboard_tag_after_insert),
            (Dashboard, "after_update", _listeners._dashboard_tag_after_update),
            (Dashboard, "after_delete", _listeners._dashboard_tag_after_delete),
            (SqlaTable, "after_insert", _listeners._dataset_tag_after_insert),
            (SqlaTable, "after_update", _listeners._dataset_tag_after_update),
            (SqlaTable, "after_delete", _listeners._dataset_tag_after_delete),
            (SavedQuery, "after_insert", _listeners._query_tag_after_insert),
            (SavedQuery, "after_update", _listeners._query_tag_after_update),
            (SavedQuery, "after_delete", _listeners._query_tag_after_delete),
            (FavStar, "after_insert", _listeners._favstar_after_insert),
            (FavStar, "after_delete", _listeners._favstar_after_delete),
        ):
            if event.contains(target, ident, fn):
                event.remove(target, ident, fn)
