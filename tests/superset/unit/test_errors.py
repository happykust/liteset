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
"""Unit tests for SupersetError and related structures:
- __post_init__ always overwrites issue_codes from the mapping (unconditional
  dict.update(), never a conditional guard)
- extra fields unrelated to issue_codes are preserved
- error types with no mapping leave extra untouched
- to_dict() omits extra when falsy
"""

from __future__ import annotations

from superset.errors import (
    ERROR_TYPES_TO_ISSUE_CODES_MAPPING,
    ErrorLevel,
    ISSUE_CODES,
    SupersetError,
    SupersetErrorType,
)

# ---------------------------------------------------------------------------
# __post_init__ — issue_codes injection
# ---------------------------------------------------------------------------


def test_post_init_sets_issue_codes_when_extra_is_none() -> None:
    """issue_codes must be injected even when extra is not provided."""
    err = SupersetError(
        message="timeout",
        error_type=SupersetErrorType.BACKEND_TIMEOUT_ERROR,
        level=ErrorLevel.ERROR,
    )
    assert err.extra is not None
    assert "issue_codes" in err.extra
    expected_codes = ERROR_TYPES_TO_ISSUE_CODES_MAPPING[
        SupersetErrorType.BACKEND_TIMEOUT_ERROR
    ]
    assert [ic["code"] for ic in err.extra["issue_codes"]] == expected_codes


def test_post_init_sets_issue_codes_when_extra_is_empty_dict() -> None:
    """issue_codes must be injected when extra={} is passed."""
    err = SupersetError(
        message="timeout",
        error_type=SupersetErrorType.BACKEND_TIMEOUT_ERROR,
        level=ErrorLevel.ERROR,
        extra={},
    )
    assert "issue_codes" in err.extra  # type: ignore[index]


def test_post_init_overwrites_caller_supplied_issue_codes() -> None:
    """__post_init__ always calls self.extra.update(), unconditionally replacing
    any issue_codes the caller may have supplied.

    Regression guard: a ``if "issue_codes" not in self.extra`` guard would have
    preserved stale/custom codes instead of the authoritative mapping-derived ones.
    """
    stale_codes = [{"code": 9999, "message": "stale"}]
    err = SupersetError(
        message="timeout",
        error_type=SupersetErrorType.BACKEND_TIMEOUT_ERROR,
        level=ErrorLevel.ERROR,
        extra={"issue_codes": stale_codes, "custom_key": "preserved"},
    )
    # issue_codes must be overwritten with the authoritative mapping-derived list
    expected_codes = ERROR_TYPES_TO_ISSUE_CODES_MAPPING[
        SupersetErrorType.BACKEND_TIMEOUT_ERROR
    ]
    assert [ic["code"] for ic in err.extra["issue_codes"]] == expected_codes  # type: ignore[index]
    # stale entry must NOT survive
    assert err.extra["issue_codes"] != stale_codes  # type: ignore[index]
    # unrelated keys must be preserved by dict.update()
    assert err.extra["custom_key"] == "preserved"  # type: ignore[index]


def test_post_init_preserves_extra_non_issue_code_keys() -> None:
    """Other extra keys must survive alongside the injected issue_codes."""
    err = SupersetError(
        message="column missing",
        error_type=SupersetErrorType.COLUMN_DOES_NOT_EXIST_ERROR,
        level=ErrorLevel.ERROR,
        extra={"engine": "postgresql", "column": "foo"},
    )
    assert err.extra is not None
    assert err.extra["engine"] == "postgresql"
    assert err.extra["column"] == "foo"
    assert "issue_codes" in err.extra


def test_post_init_no_injection_for_unmapped_error_type() -> None:
    """Error types not present in ERROR_TYPES_TO_ISSUE_CODES_MAPPING must
    leave extra completely untouched."""
    err = SupersetError(
        message="frontend error",
        error_type=SupersetErrorType.FRONTEND_CSRF_ERROR,
        level=ErrorLevel.WARNING,
        extra={"url": "/dashboard"},
    )
    # FRONTEND_CSRF_ERROR is not in the mapping
    mapping = ERROR_TYPES_TO_ISSUE_CODES_MAPPING
    assert SupersetErrorType.FRONTEND_CSRF_ERROR not in mapping
    assert err.extra == {"url": "/dashboard"}
    assert "issue_codes" not in err.extra  # type: ignore[operator]


def test_post_init_no_injection_no_extra_for_unmapped_type() -> None:
    """extra must remain None when not provided and error type has no mapping."""
    err = SupersetError(
        message="frontend timeout",
        error_type=SupersetErrorType.FRONTEND_TIMEOUT_ERROR,
        level=ErrorLevel.WARNING,
    )
    assert err.extra is None


# ---------------------------------------------------------------------------
# issue_codes format
# ---------------------------------------------------------------------------


def test_issue_codes_message_format() -> None:
    """Each issue_code entry must be ``{"code": N, "message": "Issue N - <text>"}``."""
    err = SupersetError(
        message="syntax err",
        error_type=SupersetErrorType.SYNTAX_ERROR,
        level=ErrorLevel.ERROR,
    )
    assert err.extra is not None
    for entry in err.extra["issue_codes"]:
        code = entry["code"]
        assert entry["message"] == f"Issue {code} - {ISSUE_CODES[code]}"


# ---------------------------------------------------------------------------
# to_dict()
# ---------------------------------------------------------------------------


def test_to_dict_includes_extra_when_present() -> None:
    err = SupersetError(
        message="msg",
        error_type=SupersetErrorType.GENERIC_BACKEND_ERROR,
        level=ErrorLevel.ERROR,
    )
    d = err.to_dict()
    assert d["message"] == "msg"
    assert d["error_type"] == SupersetErrorType.GENERIC_BACKEND_ERROR
    assert "extra" in d
    assert "issue_codes" in d["extra"]


def test_to_dict_omits_extra_when_none() -> None:
    """to_dict() must not include an 'extra' key when extra is falsy (None or {})."""
    err = SupersetError(
        message="frontend",
        error_type=SupersetErrorType.FRONTEND_CSRF_ERROR,
        level=ErrorLevel.WARNING,
    )
    # FRONTEND_CSRF_ERROR has no mapping so extra stays None
    d = err.to_dict()
    assert "extra" not in d
