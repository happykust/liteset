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
"""Verify Alembic configuration for Superset migrations."""

from __future__ import annotations

from pathlib import Path


def test_alembic_ini_exists():
    ini_path = Path("superset/migrations/alembic.ini")
    assert ini_path.exists(), "alembic.ini must exist in superset/migrations/"


def test_alembic_env_exists():
    env_path = Path("superset/migrations/env.py")
    assert env_path.exists(), "env.py must exist in superset/migrations/"


def test_alembic_env_uses_sync_driver():
    env_path = Path("superset/migrations/env.py")
    content = env_path.read_text()
    assert "psycopg2" in content, "env.py must reference psycopg2 sync driver"


def test_alembic_env_imports_superset_models():
    env_path = Path("superset/migrations/env.py")
    content = env_path.read_text()
    assert "superset.models" in content, "env.py must reference superset models"
