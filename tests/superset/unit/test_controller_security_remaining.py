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
"""Tests for SecurityController (remaining endpoints validation)."""

from __future__ import annotations

from superset.controllers.security import SecurityController


def test_controller_path() -> None:
    assert SecurityController.path == "/api/v1/security"


def test_controller_has_csrf_endpoint() -> None:
    assert hasattr(SecurityController, "csrf_token")


def test_controller_has_guest_token_endpoint() -> None:
    assert hasattr(SecurityController, "guest_token")


def test_controller_has_search_roles_endpoint() -> None:
    assert hasattr(SecurityController, "search_roles")
