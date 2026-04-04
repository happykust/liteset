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
"""Verify config migration completeness: all _SUPERSET_TO_LITESET entries
load correctly from superset_config.py, feature flag merge works, and
special cases (timedelta, SUPERSET_FEATURE_* env vars) are handled."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """Clear the superset_config module cache before and after each test."""
    from superset.config import _superset_config_cache

    _superset_config_cache.clear()
    yield
    _superset_config_cache.clear()


def test_mapping_count():
    """_SUPERSET_TO_LITESET must have >= 80 entries (was 23 before migration)."""
    from superset.config import _SUPERSET_TO_LITESET

    assert len(_SUPERSET_TO_LITESET) >= 80, (
        f"Expected >= 80 mappings, got {len(_SUPERSET_TO_LITESET)}"
    )


def test_all_mapped_keys_have_fields():
    """Every liteset field name in _SUPERSET_TO_LITESET must exist in SupersetSettings."""
    from superset.config import _SUPERSET_TO_LITESET, SupersetSettings

    model_fields = set(SupersetSettings.model_fields.keys())
    for sup_key, lit_key in _SUPERSET_TO_LITESET.items():
        assert lit_key in model_fields, (
            f"Mapping {sup_key!r} -> {lit_key!r} has no corresponding "
            f"field in SupersetSettings"
        )


def test_timedelta_coercion(monkeypatch: pytest.MonkeyPatch):
    """PERMANENT_SESSION_LIFETIME = timedelta(days=7) -> session_max_age = 604800."""
    from datetime import timedelta

    from superset.config import SupersetSettings

    settings = SupersetSettings(
        secret_key="test-secret-key-at-least-16",
        sqlalchemy_database_uri="sqlite:///test.db",
        session_max_age=timedelta(days=7),
    )
    assert settings.session_max_age == 604800


def test_timedelta_default():
    """Default session_max_age matches original timedelta(days=31) = 2678400."""
    from superset.config import SupersetSettings

    settings = SupersetSettings(
        secret_key="test-secret-key-at-least-16",
        sqlalchemy_database_uri="sqlite:///test.db",
    )
    assert settings.session_max_age == 2678400


def test_feature_flags_merge_defaults():
    """Feature flags include all _DEFAULT_FEATURE_FLAGS even when user provides partial."""
    from superset.config import SupersetSettings

    settings = SupersetSettings(
        secret_key="test-secret-key-at-least-16",
        sqlalchemy_database_uri="sqlite:///test.db",
        feature_flags={"MY_CUSTOM_FLAG": True},
    )
    # User flag preserved
    assert settings.feature_flags["MY_CUSTOM_FLAG"] is True
    # Defaults preserved
    assert "CSS_TEMPLATES" in settings.feature_flags
    assert settings.feature_flags["CSS_TEMPLATES"] is True
    assert "TAGGING_SYSTEM" in settings.feature_flags
    assert settings.feature_flags["TAGGING_SYSTEM"] is False


def test_feature_flags_user_overrides_default():
    """User FEATURE_FLAGS override DEFAULT_FEATURE_FLAGS."""
    from superset.config import SupersetSettings

    settings = SupersetSettings(
        secret_key="test-secret-key-at-least-16",
        sqlalchemy_database_uri="sqlite:///test.db",
        feature_flags={"TAGGING_SYSTEM": True},
    )
    assert settings.feature_flags["TAGGING_SYSTEM"] is True


def test_superset_feature_env_vars(monkeypatch: pytest.MonkeyPatch):
    """SUPERSET_FEATURE_* env vars override feature flags with highest priority."""
    monkeypatch.setenv("SUPERSET_FEATURE_TAGGING_SYSTEM", "true")
    monkeypatch.setenv("SUPERSET_FEATURE_MY_CUSTOM", "1")

    from superset.config import SupersetSettings

    settings = SupersetSettings(
        secret_key="test-secret-key-at-least-16",
        sqlalchemy_database_uri="sqlite:///test.db",
    )
    assert settings.feature_flags["TAGGING_SYSTEM"] is True
    assert settings.feature_flags["MY_CUSTOM"] is True
    # Default still preserved
    assert settings.feature_flags["CSS_TEMPLATES"] is True


def test_superset_feature_env_var_false(monkeypatch: pytest.MonkeyPatch):
    """SUPERSET_FEATURE_* env var set to false disables a flag."""
    monkeypatch.setenv("SUPERSET_FEATURE_CSS_TEMPLATES", "false")

    from superset.config import SupersetSettings

    settings = SupersetSettings(
        secret_key="test-secret-key-at-least-16",
        sqlalchemy_database_uri="sqlite:///test.db",
    )
    assert settings.feature_flags["CSS_TEMPLATES"] is False


def test_config_loads_from_file(monkeypatch: pytest.MonkeyPatch):
    """superset_config.py values load through _SUPERSET_TO_LITESET mapping."""
    config_content = """\
SECRET_KEY = "test-secret-key-at-least-16"
SQLALCHEMY_DATABASE_URI = "postgresql://u:p@host/db"
APP_NAME = "MySuperset"
SMTP_PORT = 587
AUTH_TYPE = 4
FEATURE_FLAGS = {"ALERT_REPORTS": True}
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    ) as f:
        f.write(config_content)
        config_path = f.name

    try:
        monkeypatch.setenv("SUPERSET_CONFIG_PATH", config_path)
        from superset.config import SupersetSettings

        settings = SupersetSettings()
        assert settings.app_name == "MySuperset"
        assert settings.smtp_port == 587
        assert settings.auth_type == 4
        assert settings.feature_flags["ALERT_REPORTS"] is True
        # Defaults preserved in merged flags
        assert settings.feature_flags["CSS_TEMPLATES"] is True
    finally:
        os.unlink(config_path)
