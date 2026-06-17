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
"""``_sync_user_can_access_dataset`` must honour FAB *group* membership.

``user_view_menu_names`` joins ``assoc_user_group``/``assoc_group_role``,
and ``get_user_roles`` returns ``user.roles + [role for group in user.groups
for role in group.roles]`` — a user whose only grants come from a group must
NOT be denied dataset/metric macros.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import superset.jinja_context as jc


def _dataset() -> SimpleNamespace:
    return SimpleNamespace(
        database_id=3,
        perm="[db].[tbl](id:7)",
        catalog_perm=None,
        schema_perm=None,
    )


def _user(roles=(), user_id=11):
    return SimpleNamespace(id=user_id, roles=list(roles))


def test_group_role_grants_dataset_access():
    """No direct roles; group role carries datasource_access → allowed."""
    with (
        patch.object(jc, "_sync_get_user_group_roles", return_value=[(5, "Readers")]),
        patch.object(
            jc,
            "_collect_role_perm_rows",
            return_value=[("datasource_access", "[db].[tbl](id:7)")],
        ) as collect,
    ):
        assert jc._sync_user_can_access_dataset(_dataset(), _user()) is True
    collect.assert_called_once_with([5])


def test_group_admin_short_circuits():
    """Admin inherited via a group grants full access (FAB get_user_roles)."""
    with patch.object(jc, "_sync_get_user_group_roles", return_value=[(1, "Admin")]):
        assert jc._sync_user_can_access_dataset(_dataset(), _user()) is True


def test_no_roles_anywhere_denied():
    with patch.object(jc, "_sync_get_user_group_roles", return_value=[]):
        assert jc._sync_user_can_access_dataset(_dataset(), _user()) is False


def test_direct_and_group_role_ids_are_merged():
    direct_role = SimpleNamespace(id=2, name="Gamma")
    with (
        patch.object(jc, "_sync_get_user_group_roles", return_value=[(5, "Readers")]),
        patch.object(jc, "_collect_role_perm_rows", return_value=[]) as collect,
    ):
        jc._sync_user_can_access_dataset(_dataset(), _user(roles=[direct_role]))
    collect.assert_called_once_with([2, 5])


def test_group_lookup_failure_falls_back_to_direct_roles():
    """A failing group query must not break direct-role access (fail-soft)."""
    direct_role = SimpleNamespace(id=2, name="Gamma")
    with (
        patch.object(
            jc, "_sync_get_user_group_roles", side_effect=RuntimeError("db down")
        ),
        patch.object(
            jc,
            "_collect_role_perm_rows",
            return_value=[("all_datasource_access", None)],
        ),
    ):
        assert (
            jc._sync_user_can_access_dataset(_dataset(), _user(roles=[direct_role]))
            is True
        )
