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
"""Tests that real user objects (CachedUser, GuestUser) carry permissions for RBAC."""

from __future__ import annotations

from superset.guards.rbac import has_permissions
from superset.middleware.auth import CachedUser
from superset.security.guest import GuestUser


def test_cached_user_has_permissions():
    user = CachedUser(
        id=1,
        username="admin",
        permissions={("can_read", "Chart"), ("can_write", "Chart")},
    )
    assert has_permissions(user, {("can_read", "Chart")})


def test_cached_user_without_permission_fails():
    user = CachedUser(id=1, username="admin", permissions={("can_read", "Chart")})
    assert not has_permissions(user, {("can_write", "Dashboard")})


def test_empty_permissions_denies_all():
    user = CachedUser(id=1, username="viewer", permissions=set())
    assert not has_permissions(user, {("can_read", "Chart")})


def test_cached_user_default_permissions_empty():
    user = CachedUser(id=1, username="viewer")
    assert user.permissions == set()
    assert not has_permissions(user, {("can_read", "Chart")})


def test_cached_user_from_dict_preserves_permissions():
    data = {
        "id": 1,
        "username": "admin",
        "permissions": [["can_read", "Chart"], ["can_write", "Chart"]],
        "roles": [],
    }
    user = CachedUser.from_dict(data)
    assert user is not None
    assert user.permissions == {("can_read", "Chart"), ("can_write", "Chart")}
    assert has_permissions(user, {("can_read", "Chart")})


def test_cached_user_from_dict_missing_permissions():
    data = {"id": 1, "username": "admin", "roles": []}
    user = CachedUser.from_dict(data)
    assert user is not None
    assert user.permissions == set()


def test_guest_user_dashboard_permissions():
    payload = {
        "user": {"username": "guest"},
        "resources": [{"type": "dashboard", "id": "abc-123"}],
        "rls_rules": [],
    }
    guest = GuestUser.from_token_payload(payload)
    assert has_permissions(guest, {("can_read", "Dashboard")})
    assert has_permissions(guest, {("can_read", "Chart")})
    assert not has_permissions(guest, {("can_write", "Dashboard")})


def test_guest_user_chart_permissions():
    payload = {
        "user": {"username": "guest"},
        "resources": [{"type": "chart", "id": "xyz-456"}],
        "rls_rules": [],
    }
    guest = GuestUser.from_token_payload(payload)
    assert has_permissions(guest, {("can_read", "Chart")})
    assert not has_permissions(guest, {("can_read", "Dashboard")})


def test_guest_user_no_resources_no_permissions():
    payload = {
        "user": {"username": "guest"},
        "resources": [],
        "rls_rules": [],
    }
    guest = GuestUser.from_token_payload(payload)
    assert guest.permissions == set()
    assert not has_permissions(guest, {("can_read", "Chart")})
