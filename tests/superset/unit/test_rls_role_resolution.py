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
"""Tests for ``_sync_resolve_user_role_ids`` (RLS role selection).

Only ``g.user is None`` skips RLS; an *anonymous* user proceeds with an
empty role list so BASE filters still apply.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from superset.utils import rls as rls_module


def _settings(auth_role_public):
    return SimpleNamespace(auth_role_public=auth_role_public)


def test_none_user_skips_rls():
    # ``g.user is None`` → None → caller returns [] (no RLS at all).
    assert rls_module._sync_resolve_user_role_ids(None) is None


def test_anonymous_no_public_role_returns_empty_list():
    # Anonymous + AUTH_ROLE_PUBLIC unset → [] (NOT None): empty roles mean
    # all BASE filters apply, matching upstream notin_([]) semantics.
    anon = SimpleNamespace(is_anonymous=True, is_authenticated=False)
    with patch.object(rls_module, "_cached_settings", return_value=_settings("")):
        assert rls_module._sync_resolve_user_role_ids(anon) == []


def test_anonymous_with_public_role_resolves_role_id():
    anon = SimpleNamespace(is_anonymous=True, is_authenticated=False)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, _stmt):
            class _R:
                def scalars(self):
                    return self

                def one_or_none(self):
                    return SimpleNamespace(id=42)

            return _R()

    with (
        patch.object(rls_module, "_cached_settings", return_value=_settings("Public")),
        patch.object(rls_module, "_metadata_sync_session", return_value=_Session()),
    ):
        assert rls_module._sync_resolve_user_role_ids(anon) == [42]


def test_anonymous_public_role_missing_returns_empty_list():
    # Configured role deleted from the DB → [] (strictest), never crash.
    anon = SimpleNamespace(is_anonymous=True, is_authenticated=False)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, _stmt):
            class _R:
                def scalars(self):
                    return self

                def one_or_none(self):
                    return None

            return _R()

    with (
        patch.object(rls_module, "_cached_settings", return_value=_settings("Public")),
        patch.object(rls_module, "_metadata_sync_session", return_value=_Session()),
    ):
        assert rls_module._sync_resolve_user_role_ids(anon) == []


def test_authenticated_user_returns_role_ids():
    # No ``id`` → no group lookup; direct roles only.
    user = SimpleNamespace(
        is_anonymous=False,
        is_authenticated=True,
        roles=[SimpleNamespace(id=1), SimpleNamespace(id=7)],
    )
    assert sorted(rls_module._sync_resolve_user_role_ids(user)) == [1, 7]


def test_authenticated_user_includes_group_roles():
    """RLS role resolution must include group-inherited roles (direct + group.roles).

    A role assigned only via a group was previously omitted, so a REGULAR RLS
    filter scoped to it never matched and the row restriction was silently
    skipped (data exposure).
    """
    user = SimpleNamespace(
        is_anonymous=False,
        is_authenticated=True,
        id=1,
        roles=[SimpleNamespace(id=2)],
    )

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, _stmt):
            class _R:
                def scalars(self):
                    return [5]  # group-derived role id

            return _R()

    with patch.object(rls_module, "_metadata_sync_session", return_value=_Session()):
        result = rls_module._sync_resolve_user_role_ids(user)
    assert set(result) == {2, 5}
