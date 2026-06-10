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
"""Unit tests for superset.utils.hashing — verifies 1:1 parity with original."""

from __future__ import annotations

import math
from unittest.mock import patch

from superset.utils.hashing import md5_sha_from_dict, md5_sha_from_str


def test_basic_str_hash():
    """md5_sha_from_str produces a stable, known MD5 hex digest."""
    result = md5_sha_from_str("hello")
    assert result == "5d41402abc4b2a76b9719d911017c592"


def test_basic_dict_hash():
    """md5_sha_from_dict produces the same hash regardless of insertion order."""
    obj1 = {"product": "Coffee", "price": 100, "company": "ACME"}
    obj2 = {"company": "ACME", "product": "Coffee", "price": 100}
    assert md5_sha_from_dict(obj1) == md5_sha_from_dict(obj2)


def test_dict_hash_matches_str_hash():
    """md5_sha_from_dict result equals md5_sha_from_str on the sorted JSON string."""
    obj = {"a": 1, "b": 2}
    serialized = '{"a": 1, "b": 2}'
    assert md5_sha_from_dict(obj) == md5_sha_from_str(serialized)


def test_bytes_value_in_dict_succeeds():
    """Dict with a bytes value must be hashed without raising TypeError.

    The original superset.utils.json.dumps wrapper passes encoding="utf-8" to
    simplejson, which decodes bytes values as UTF-8 strings before serialising.
    Direct simplejson.dumps without encoding raises TypeError for bytes values
    in Python 3.  This test guards against regressions that bypass the wrapper.
    """
    obj = {"key": b"hello"}
    # Must not raise — original behaviour routes through json.dumps wrapper
    result = md5_sha_from_dict(obj)
    assert isinstance(result, str)
    assert len(result) == 32  # MD5 hex digest length


def test_bytes_value_stable_hash():
    """Bytes value b'hello' hashes identically to string 'hello' via utf-8 encoding."""
    hash_bytes = md5_sha_from_dict({"key": b"hello"})
    hash_str = md5_sha_from_dict({"key": "hello"})
    # simplejson with encoding="utf-8" decodes bytes to str before serialising,
    # so the resulting JSON — and therefore the hash — must be identical.
    assert hash_bytes == hash_str


def test_ignore_nan_false_keeps_nan():
    """ignore_nan=False (default) preserves NaN in JSON output."""
    obj = {"v": math.nan}
    serialized_nan = '{"v": NaN}'
    assert md5_sha_from_dict(obj) == md5_sha_from_str(serialized_nan)


def test_ignore_nan_true_replaces_nan_with_null():
    """ignore_nan=True replaces NaN with null in the JSON output."""
    obj = {"v": math.nan}
    serialized_null = '{"v": null}'
    assert md5_sha_from_dict(obj, ignore_nan=True) == md5_sha_from_str(serialized_null)


def test_custom_default_serializer():
    """A custom default serializer is forwarded to the underlying json.dumps call."""

    class _Sentinel:
        pass

    def custom_default(o):
        if isinstance(o, _Sentinel):
            return "SENTINEL"
        raise TypeError(f"Not serialisable: {o!r}")

    obj = {"s": _Sentinel()}
    result = md5_sha_from_dict(obj, default=custom_default)
    assert result == md5_sha_from_str('{"s": "SENTINEL"}')


def test_unicode_decode_error_retry():
    """UnicodeDecodeError from encoding="utf-8" is retried with encoding=None.

    The original json.dumps wrapper (superset/utils/json.py) catches
    UnicodeDecodeError on the first simplejson.dumps attempt and retries with
    encoding=None.  We verify that md5_sha_from_dict does not propagate
    UnicodeDecodeError to the caller (i.e. the wrapper is used, not raw
    simplejson.dumps).
    """
    import simplejson

    call_count = 0
    original_dumps = simplejson.dumps

    def patched_dumps(obj, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1 and kwargs.get("encoding") == "utf-8":
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "test injection")
        return original_dumps(obj, **kwargs)

    with patch("simplejson.dumps", side_effect=patched_dumps):
        # Must complete without propagating UnicodeDecodeError
        result = md5_sha_from_dict({"k": "v"})

    assert isinstance(result, str)
    assert len(result) == 32
    # Confirm the retry path was reached (two calls: first raises, second succeeds)
    assert call_count == 2
