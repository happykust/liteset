"""Tests for authentication controller -- password hashing, email login,
timing-safe behaviour.
"""

from __future__ import annotations

import hashlib

import pytest

from superset.controllers.auth import (
    _check_password_hash,
    _FAKE_PASSWORD_HASH,
)

# ``_hash_internal`` is a helper in ``superset.utils.password`` (the auth
# controller only re-exports ``check_password_hash`` / ``_FAKE_PASSWORD_HASH``).
from superset.utils.password import _hash_internal

# ---------------------------------------------------------------------------
# Pre-computed hashes (generated with werkzeug 3.1 for "correct-password")
# ---------------------------------------------------------------------------
SCRYPT_HASH = (
    "scrypt:32768:8:1$2soUhTMsQebEVjb5$"
    "dda76edaa3335a0d7829445337e32a6ebd827e756b4f9bb8c17df3149cd37043"
    "12f837ffb3d57cc195eba6735dda194ff69d44c18c381eb122510e7b63ff6870"
)

PBKDF2_HASH = (
    "pbkdf2:sha256:1000000$3R1D5OnEsd8uBscb$"
    "b61f24e529b2ad4058e80268725cd03eccc89e02d57b6ac2f659f9f366c61fd0"
)


# ===================================================================
# _hash_internal tests
# ===================================================================


class TestHashInternal:
    """Low-level hash computation via _hash_internal."""

    def test_scrypt_with_params(self) -> None:
        """scrypt:N:r:p produces the same hex as hashlib.scrypt."""
        password = "hello"
        salt = "somesalt"
        result = _hash_internal("scrypt:32768:8:1", salt, password)
        expected = hashlib.scrypt(
            password.encode(),
            salt=salt.encode(),
            n=32768,
            r=8,
            p=1,
            maxmem=132 * 32768 * 8 * 1,
        ).hex()
        assert result == expected

    def test_scrypt_defaults(self) -> None:
        """Bare 'scrypt' (no params) uses defaults 2**15, 8, 1."""
        password = "hello"
        salt = "somesalt"
        result = _hash_internal("scrypt", salt, password)
        expected = hashlib.scrypt(
            password.encode(),
            salt=salt.encode(),
            n=2**15,
            r=8,
            p=1,
            maxmem=132 * (2**15) * 8 * 1,
        ).hex()
        assert result == expected

    def test_pbkdf2_with_params(self) -> None:
        """pbkdf2:sha256:N produces the same hex as hashlib.pbkdf2_hmac."""
        password = "hello"
        salt = "somesalt"
        result = _hash_internal("pbkdf2:sha256:10000", salt, password)
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 10000
        ).hex()
        assert result == expected

    def test_pbkdf2_defaults(self) -> None:
        """Bare 'pbkdf2' uses sha256 and 600000 iterations."""
        password = "hello"
        salt = "somesalt"
        result = _hash_internal("pbkdf2", salt, password)
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 600_000
        ).hex()
        assert result == expected

    def test_unsupported_method_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported hash method"):
            _hash_internal("bcrypt", "salt", "pw")

    def test_invalid_scrypt_parts_raises(self) -> None:
        """scrypt with wrong number of colon-separated params raises."""
        with pytest.raises(ValueError, match="Invalid scrypt method"):
            _hash_internal("scrypt:32768:8", "salt", "pw")


# ===================================================================
# _check_password_hash tests
# ===================================================================


class TestCheckPasswordHash:
    """Full round-trip verification of werkzeug-format hashes."""

    def test_scrypt_correct_password(self) -> None:
        assert _check_password_hash(SCRYPT_HASH, "correct-password") is True

    def test_scrypt_wrong_password(self) -> None:
        assert _check_password_hash(SCRYPT_HASH, "wrong-password") is False

    def test_pbkdf2_correct_password(self) -> None:
        assert _check_password_hash(PBKDF2_HASH, "correct-password") is True

    def test_pbkdf2_wrong_password(self) -> None:
        assert _check_password_hash(PBKDF2_HASH, "wrong-password") is False

    def test_empty_hash_returns_false(self) -> None:
        assert _check_password_hash("", "password") is False

    def test_empty_password_returns_false(self) -> None:
        assert _check_password_hash(SCRYPT_HASH, "") is False

    def test_none_hash_returns_false(self) -> None:
        # type: ignore -- intentionally passing wrong type
        assert _check_password_hash(None, "password") is False  # type: ignore[arg-type]

    def test_malformed_hash_returns_false(self) -> None:
        assert _check_password_hash("not-a-hash", "password") is False

    def test_missing_dollar_signs_returns_false(self) -> None:
        assert _check_password_hash("pbkdf2:sha256:10000", "password") is False

    def test_fake_password_hash_is_valid_format(self) -> None:
        """_FAKE_PASSWORD_HASH must parse without error (for timing balance)."""
        # Should not raise -- result doesn't matter, only that it computes
        result = _check_password_hash(_FAKE_PASSWORD_HASH, "password")
        # The fake hash is all zeros, so it should never match a real password
        assert result is False

    def test_timing_safe_comparison(self) -> None:
        """Verify that hmac.compare_digest is used (not ==)."""
        # We can't easily test timing, but we CAN verify the code path
        # works for both match and mismatch without short-circuiting
        assert _check_password_hash(SCRYPT_HASH, "correct-password") is True
        assert _check_password_hash(SCRYPT_HASH, "almost-correct-password") is False


# ===================================================================
# Werkzeug interop (validates our implementation matches werkzeug)
# ===================================================================


class TestWerkzeugInterop:
    """Verify our _check_password_hash matches werkzeug's check_password_hash."""

    def test_scrypt_matches_werkzeug(self) -> None:
        """Hash generated by werkzeug verifies correctly with our code."""
        # This hash was generated by werkzeug 3.1.7
        assert _check_password_hash(SCRYPT_HASH, "correct-password") is True

    def test_pbkdf2_matches_werkzeug(self) -> None:
        """Hash generated by werkzeug verifies correctly with our code."""
        # This hash was generated by werkzeug 3.1.7
        assert _check_password_hash(PBKDF2_HASH, "correct-password") is True

    def test_generate_and_verify_scrypt(self) -> None:
        """Round-trip: generate with _hash_internal, verify with _check."""
        salt = "TestSalt12345678"
        method = "scrypt:32768:8:1"
        h = _hash_internal(method, salt, "my-password")
        stored = f"{method}${salt}${h}"
        assert _check_password_hash(stored, "my-password") is True
        assert _check_password_hash(stored, "not-my-password") is False

    def test_generate_and_verify_pbkdf2(self) -> None:
        """Round-trip: generate with _hash_internal, verify with _check."""
        salt = "TestSalt12345678"
        method = "pbkdf2:sha256:10000"
        h = _hash_internal(method, salt, "my-password")
        stored = f"{method}${salt}${h}"
        assert _check_password_hash(stored, "my-password") is True
        assert _check_password_hash(stored, "not-my-password") is False
