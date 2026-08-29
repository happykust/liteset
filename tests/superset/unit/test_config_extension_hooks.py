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
"""``CUSTOM_SECURITY_MANAGER`` resolution.

The setting used to be declared in config and read nowhere, so a fork's
custom manager was silently dropped and the deployment ran with different
authorization rules than its config asked for.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from superset.security.manager import (
    _resolve_custom_security_manager,
    AsyncSecurityManager,
    build_async_security_manager,
)


class CustomManager(AsyncSecurityManager):
    """A fork's own manager."""


def _settings(**overrides):
    base = {
        "custom_security_manager": None,
        "auth_role_admin": "Admin",
        "auth_role_public": "",
        "guest_role_name": "Public",
        "dashboard_rbac": False,
        "embedded_superset": False,
        "feature_flags": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_defaults_to_builtin_manager() -> None:
    assert _resolve_custom_security_manager(_settings()) is AsyncSecurityManager


def test_accepts_a_class() -> None:
    settings = _settings(custom_security_manager=CustomManager)
    assert _resolve_custom_security_manager(settings) is CustomManager


def test_accepts_a_dotted_path() -> None:
    settings = _settings(
        custom_security_manager=f"{__name__}.CustomManager",
    )
    assert _resolve_custom_security_manager(settings) is CustomManager


def test_builder_instantiates_the_custom_manager() -> None:
    settings = _settings(custom_security_manager=CustomManager)
    manager = build_async_security_manager(MagicMock(), settings)
    assert isinstance(manager, CustomManager)


def test_rejects_an_incompatible_class() -> None:
    """A Flask-AppBuilder-era manager has an incompatible interface."""

    class LegacyFabManager:
        pass

    settings = _settings(custom_security_manager=LegacyFabManager)
    with pytest.raises(TypeError, match="AsyncSecurityManager"):
        _resolve_custom_security_manager(settings)


def test_rejects_a_bare_name() -> None:
    settings = _settings(custom_security_manager="NotADottedPath")
    with pytest.raises(ValueError, match="dotted path"):
        _resolve_custom_security_manager(settings)
