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
"""Tests for liteset.utils.json — local JSON serialization utilities."""

from __future__ import annotations

import datetime
import decimal
import uuid

import numpy as np
import pytest

from liteset.utils.json import dumps, loads


def test_dumps_datetime():
    dt = datetime.datetime(2026, 1, 15, 12, 0, 0)
    result = dumps({"ts": dt})
    assert "2026-01-15" in result


def test_dumps_date():
    d = datetime.date(2026, 3, 24)
    result = dumps({"d": d})
    assert "2026-03-24" in result


def test_dumps_uuid():
    u = uuid.uuid4()
    result = dumps({"id": u})
    assert str(u) in result


def test_dumps_decimal():
    result = dumps({"val": decimal.Decimal("3.14")})
    assert "3.14" in result


def test_dumps_bytes():
    result = dumps({"b": b"hello"})
    assert "hello" in result


def test_dumps_numpy_integer():
    result = dumps({"n": np.int64(42)})
    assert "42" in result


def test_dumps_numpy_floating():
    result = dumps({"f": np.float64(1.5)})
    assert "1.5" in result


def test_dumps_numpy_ndarray():
    result = dumps({"arr": np.array([1, 2, 3])})
    assert "[1, 2, 3]" in result


def test_dumps_numpy_bool():
    result = dumps({"flag": np.bool_(True)})
    assert "true" in result


def test_dumps_compact():
    result = dumps({"a": 1}, indent=None, separators=(",", ":"), sort_keys=True)
    assert result == '{"a":1}'


def test_loads_roundtrip():
    data = {"key": "value", "num": 42}
    assert loads(dumps(data)) == data


def test_loads_bytes():
    data = b'{"x": 1}'
    assert loads(data) == {"x": 1}


def test_dumps_unserializable_raises():
    class Custom:
        pass

    with pytest.raises(TypeError):
        dumps({"obj": Custom()})
