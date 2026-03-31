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
"""Werkzeug-compatible password hashing without werkzeug dependency.

Supports the two formats FAB / werkzeug may produce:
- ``scrypt:N:r:p$salt$hash``  (werkzeug >= 3.0 default)
- ``pbkdf2:hash_name:iterations$salt$hash``  (werkzeug < 3.0 default)
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_SALT_CHARS = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
)


def _gen_salt(length: int = 16) -> str:
    """Generate a random salt string (matches werkzeug gen_salt)."""
    return "".join(secrets.choice(_SALT_CHARS) for _ in range(length))


def _hash_internal(method: str, salt: str, password: str) -> str:
    """Compute a password hash for the given method and salt.

    Re-implements werkzeug's internal hashing using only :mod:`hashlib`.
    """
    parts = method.split(":")
    algo = parts[0]
    salt_bytes = salt.encode("utf-8")
    password_bytes = password.encode("utf-8")

    if algo == "scrypt":
        if len(parts) == 4:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        elif len(parts) == 1:
            n, r, p = 2**15, 8, 1
        else:
            raise ValueError(f"Invalid scrypt method: {method}")
        maxmem = 132 * n * r * p
        return hashlib.scrypt(
            password_bytes, salt=salt_bytes, n=n, r=r, p=p, maxmem=maxmem
        ).hex()

    if algo == "pbkdf2":
        hash_name = parts[1] if len(parts) >= 2 else "sha256"
        iterations = int(parts[2]) if len(parts) >= 3 else 600_000
        return hashlib.pbkdf2_hmac(
            hash_name, password_bytes, salt_bytes, iterations
        ).hex()

    raise ValueError(f"Unsupported hash method: {algo}")


def generate_password_hash(
    password: str,
    method: str = "scrypt",
    salt_length: int = 16,
) -> str:
    """Hash a password producing a werkzeug-compatible string.

    Default method is ``scrypt:32768:8:1`` (werkzeug 3.0+ default).
    Output format: ``method$salt$hash``.
    """
    salt = _gen_salt(salt_length)
    password_bytes = password.encode("utf-8")
    salt_bytes = salt.encode("utf-8")

    if method == "scrypt" or method.startswith("scrypt:"):
        parts = method.split(":")
        n = int(parts[1]) if len(parts) > 1 else 32768
        r = int(parts[2]) if len(parts) > 2 else 8
        p = int(parts[3]) if len(parts) > 3 else 1
        maxmem = 132 * n * r * p
        h = hashlib.scrypt(
            password_bytes, salt=salt_bytes, n=n, r=r, p=p, maxmem=maxmem
        ).hex()
        actual_method = f"scrypt:{n}:{r}:{p}"
    elif method == "pbkdf2" or method.startswith("pbkdf2:"):
        parts = method.split(":")
        hash_name = parts[1] if len(parts) > 1 else "sha256"
        iterations = int(parts[2]) if len(parts) > 2 else 600_000
        h = hashlib.pbkdf2_hmac(
            hash_name, password_bytes, salt_bytes, iterations
        ).hex()
        actual_method = f"pbkdf2:{hash_name}:{iterations}"
    else:
        raise ValueError(f"Unsupported hash method: {method!r}")

    return f"{actual_method}${salt}${h}"


def check_password_hash(stored_hash: str, password: str) -> bool:
    """Verify a werkzeug-style password hash.

    Uses :func:`hmac.compare_digest` for timing-safe comparison.
    """
    if not stored_hash or not password:
        return False
    try:
        method, salt, hash_value = stored_hash.split("$", 2)
        computed = _hash_internal(method, salt, password)
        return hmac.compare_digest(computed, hash_value)
    except (ValueError, IndexError, AttributeError):
        return False
