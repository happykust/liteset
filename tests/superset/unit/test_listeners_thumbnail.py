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
"""Unit tests for the thumbnail-triggering SA event listeners.

Specifically covers the ``is_anonymous`` default fix:
``getattr(user, "is_anonymous", False)`` — a user object that has no
``is_anonymous`` attribute (e.g. ``CachedUser``, which only carries
``is_authenticated``) must NOT be treated as anonymous.  The correct
default is ``False`` (not anonymous), consistent with every other
callsite in the codebase (``security/manager.py``, ``utils/rls.py``).

The original reads ``g.user.is_anonymous`` directly; FAB always defines
that property returning ``False`` for normal users, so the default only
matters in Liteset where the user may be a ``CachedUser``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_slice(slice_id: int = 1) -> MagicMock:
    s = MagicMock()
    s.id = slice_id
    return s


def _make_dashboard(dashboard_id: int = 2) -> MagicMock:
    d = MagicMock()
    d.id = dashboard_id
    return d


def _run_slice_listener(user_obj: object) -> MagicMock:
    from superset.models._listeners import _slice_after_changed

    mock_task = MagicMock()
    with (
        patch(
            "superset.models._listeners.cache_chart_thumbnail",
            mock_task,
            create=True,
        ),
        patch(
            "superset.utils.core.get_current_user",
            return_value=user_obj,
        ),
        patch(
            "superset.tasks.thumbnails.cache_chart_thumbnail",
            mock_task,
        ),
    ):
        _slice_after_changed(MagicMock(), MagicMock(), _make_slice())
    return mock_task


def _run_dashboard_listener(user_obj: object) -> MagicMock:
    from superset.models._listeners import _dashboard_after_changed

    mock_task = MagicMock()
    with (
        patch(
            "superset.models._listeners.cache_dashboard_thumbnail",
            mock_task,
            create=True,
        ),
        patch(
            "superset.utils.core.get_current_user",
            return_value=user_obj,
        ),
        patch(
            "superset.tasks.thumbnails.cache_dashboard_thumbnail",
            mock_task,
        ),
    ):
        _dashboard_after_changed(MagicMock(), MagicMock(), _make_dashboard())
    return mock_task


def test_slice_thumbnail_authenticated_fab_user_passes_username():
    """A FAB-style user with ``is_anonymous=False`` → username forwarded."""
    user = SimpleNamespace(username="alice", is_anonymous=False)
    task = _run_slice_listener(user)
    task.delay.assert_called_once()
    _, kwargs = task.delay.call_args
    assert kwargs["current_user"] == "alice"


def test_slice_thumbnail_anonymous_fab_user_passes_none():
    """A FAB anonymous user (``is_anonymous=True``) → ``current_user=None``."""
    user = SimpleNamespace(username="anon", is_anonymous=True)
    task = _run_slice_listener(user)
    task.delay.assert_called_once()
    _, kwargs = task.delay.call_args
    assert kwargs["current_user"] is None


def test_slice_thumbnail_cached_user_no_is_anonymous_passes_username():
    """CachedUser has no ``is_anonymous`` attribute — must NOT be anonymous.

    This is the regression guard: ``getattr(user, "is_anonymous", True)``
    would silently drop the username.  With ``False`` as the default,
    the authenticated CachedUser's username is forwarded correctly.
    """
    user = SimpleNamespace(username="bob", is_authenticated=True)
    assert not hasattr(user, "is_anonymous"), "precondition: no is_anonymous"
    task = _run_slice_listener(user)
    task.delay.assert_called_once()
    _, kwargs = task.delay.call_args
    assert kwargs["current_user"] == "bob"


def test_slice_thumbnail_none_user_passes_none():
    """No user in context → ``current_user=None``."""
    task = _run_slice_listener(None)
    task.delay.assert_called_once()
    _, kwargs = task.delay.call_args
    assert kwargs["current_user"] is None


def test_dashboard_thumbnail_authenticated_fab_user_passes_username():
    """A FAB-style user with ``is_anonymous=False`` → username forwarded."""
    user = SimpleNamespace(username="carol", is_anonymous=False)
    task = _run_dashboard_listener(user)
    task.delay.assert_called_once()
    _, kwargs = task.delay.call_args
    assert kwargs["current_user"] == "carol"


def test_dashboard_thumbnail_anonymous_fab_user_passes_none():
    """A FAB anonymous user (``is_anonymous=True``) → ``current_user=None``."""
    user = SimpleNamespace(username="anon", is_anonymous=True)
    task = _run_dashboard_listener(user)
    task.delay.assert_called_once()
    _, kwargs = task.delay.call_args
    assert kwargs["current_user"] is None


def test_dashboard_thumbnail_cached_user_no_is_anonymous_passes_username():
    """CachedUser has no ``is_anonymous`` attribute — must NOT be anonymous.

    Regression guard for the ``getattr(user, "is_anonymous", True)`` bug.
    """
    user = SimpleNamespace(username="dave", is_authenticated=True)
    assert not hasattr(user, "is_anonymous"), "precondition: no is_anonymous"
    task = _run_dashboard_listener(user)
    task.delay.assert_called_once()
    _, kwargs = task.delay.call_args
    assert kwargs["current_user"] == "dave"


def test_dashboard_thumbnail_none_user_passes_none():
    """No user in context → ``current_user=None``."""
    task = _run_dashboard_listener(None)
    task.delay.assert_called_once()
    _, kwargs = task.delay.call_args
    assert kwargs["current_user"] is None
