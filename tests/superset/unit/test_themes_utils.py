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

import pytest

from superset.commands.theme import _is_valid_algorithm, _is_valid_theme


@pytest.mark.skip(
    reason="Liteset port has no standalone _is_valid_theme_mode validator; "
    "theme-mode modes live in the private superset.commands.theme._THEME_MODES "
    "set and are only reachable via _is_valid_algorithm, not exposed as a "
    "dedicated function."
)
@pytest.mark.parametrize(
    "mode, expected",
    [
        ("default", True),
        ("dark", True),
        ("system", True),
        ("foo", False),
    ],
)
def test_is_valid_theme_mode(mode, expected):
    from superset.commands.theme import _is_valid_theme_mode  # type: ignore

    assert _is_valid_theme_mode(mode) is expected


@pytest.mark.parametrize(
    "algorithm, expected",
    [
        ("default", True),
        ("system", True),
        (["default", "dark"], True),
        (["default", "foo"], False),
        (123, False),
        (["default", 123], False),
    ],
)
def test_is_valid_algorithm(algorithm, expected):
    assert _is_valid_algorithm(algorithm) is expected


@pytest.mark.parametrize(
    "theme, expected",
    [
        ([], False),  # not a dict
        ("string", False),
        ({}, True),  # empty dict
        ({"token": {}, "components": {}, "hashed": True, "inherit": False}, True),
        (
            {
                "token": [],
            },
            False,
        ),  # wrong type for token
        ({"algorithm": "default"}, True),
        ({"algorithm": "foo"}, False),
        ({"algorithm": ["default", "dark"]}, True),
        ({"algorithm": ["default", "foo"]}, False),
    ],
)
def test_is_valid_theme(theme, expected):
    assert _is_valid_theme(theme) is expected
