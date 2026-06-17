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
"""Unit tests for cast_to_num and related column-cast helpers
in superset/utils/core.py."""

from __future__ import annotations

from superset.utils.column import cast_to_num


def test_cast_to_num_trailing_space_returns_float() -> None:
    # '1 ' — isdigit() is False (trailing space), so float() path: returns 1.0.
    result = cast_to_num("1 ")
    assert result == 1.0
    assert isinstance(result, float)


def test_cast_to_num_leading_space_returns_float() -> None:
    # ' 2' — isdigit() is False (leading space), so float() path: returns 2.0.
    result = cast_to_num(" 2")
    assert result == 2.0
    assert isinstance(result, float)


def test_cast_to_num_pure_digit_returns_int() -> None:
    # '5' — isdigit() is True, so int() path: returns 5 (int).
    result = cast_to_num("5")
    assert result == 5
    assert isinstance(result, int)


def test_cast_to_num_decimal_string_returns_float() -> None:
    # '5.2' — isdigit() is False (decimal point), float() path: returns 5.2.
    result = cast_to_num("5.2")
    assert result == 5.2
    assert isinstance(result, float)


def test_cast_to_num_int_passthrough() -> None:
    assert cast_to_num(10) == 10
    assert isinstance(cast_to_num(10), int)


def test_cast_to_num_float_passthrough() -> None:
    assert cast_to_num(10.1) == 10.1
    assert isinstance(cast_to_num(10.1), float)


def test_cast_to_num_none_returns_none() -> None:
    assert cast_to_num(None) is None


def test_cast_to_num_non_numeric_string_returns_none() -> None:
    assert cast_to_num("this is not a string") is None


def test_cast_to_num_empty_string_returns_none() -> None:
    # isdigit() False, float('') raises ValueError, so returns None.
    assert cast_to_num("") is None
