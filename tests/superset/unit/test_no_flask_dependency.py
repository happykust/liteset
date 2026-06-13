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
"""Verify the Flask ecosystem is fully removed.

Flask AND its ecosystem (flask-appbuilder, flask-login, flask-babel,
flask-caching, werkzeug, …) must not appear in the Liteset runtime or its
declared dependencies. ``superset_old/`` is the vendored upstream reference
and is intentionally excluded.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Top-level package names that belong to the Flask ecosystem. ``werkzeug`` is
# Flask's WSGI foundation and counts as part of it.
_FORBIDDEN_PREFIXES = ("flask", "werkzeug")


def _is_forbidden(module: str) -> bool:
    root = module.split(".", 1)[0]
    return root in _FORBIDDEN_PREFIXES


def test_no_flask_ecosystem_imports_in_superset():
    """No superset/ module may import flask or werkzeug."""
    violations: list[str] = []
    for py_file in Path("superset").rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden(alias.name):
                        violations.append(f"{py_file}: import {alias.name}")
            if isinstance(node, ast.ImportFrom) and node.module:
                if _is_forbidden(node.module):
                    violations.append(f"{py_file}: from {node.module}")
    assert not violations, f"Flask-ecosystem imports found in superset/: {violations}"


def test_no_flask_ecosystem_in_requirements():
    """No requirements/*.txt may pin flask or werkzeug."""
    violations: list[str] = []
    for req_file in Path("requirements").glob("*.txt"):
        for raw in req_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            pkg = line.split("==")[0].split(">=")[0].split("[")[0].strip().lower()
            if pkg in _FORBIDDEN_PREFIXES or pkg.startswith("flask-"):
                violations.append(f"{req_file}: {line}")
    assert not violations, f"Flask-ecosystem deps found: {violations}"


def test_fallback_module_removed():
    """superset/fallback.py should no longer exist."""
    assert not Path("superset/fallback.py").exists(), "fallback.py must be deleted"


def test_app_factory_no_flask_fallback():
    """create_app should not accept enable_flask_fallback parameter."""
    import inspect

    from superset.app import create_app

    sig = inspect.signature(create_app)
    assert "enable_flask_fallback" not in sig.parameters, (
        "create_app should not have enable_flask_fallback parameter"
    )
