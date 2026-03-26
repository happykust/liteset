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
"""Verify Flask dependency is fully removed."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


def test_no_flask_imports_in_liteset():
    """No liteset/ module should import from flask."""
    violations: list[str] = []
    for py_file in Path("liteset").rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("flask"):
                        violations.append(f"{py_file}: import {alias.name}")
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("flask"):
                    violations.append(f"{py_file}: from {node.module}")
    assert not violations, f"Flask imports found in liteset/: {violations}"


def test_fallback_module_removed():
    """liteset/fallback.py should no longer exist."""
    assert not Path("liteset/fallback.py").exists(), "fallback.py must be deleted"


def test_app_factory_no_flask_fallback():
    """create_app should not accept enable_flask_fallback parameter."""
    import inspect

    from liteset.app import create_app

    sig = inspect.signature(create_app)
    assert "enable_flask_fallback" not in sig.parameters, (
        "create_app should not have enable_flask_fallback parameter"
    )
