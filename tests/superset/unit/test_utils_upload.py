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
"""Unit tests for superset.utils.upload.parse_upload_form validation.

Mirrors the UploadPostSchema Range/OneOf constraints that upstream Marshmallow
enforced (rejecting with a 422 before the value reaches pandas)."""

from __future__ import annotations

import pytest

from superset.exceptions import CommandInvalidError
from superset.utils.upload import parse_upload_form


def test_rows_to_read_zero_rejected():
    """rows_to_read < 1 is rejected (upstream Range(min=1)); otherwise an empty
    DataFrame would silently overwrite the target table."""
    with pytest.raises(CommandInvalidError, match="rows_to_read"):
        parse_upload_form({"rows_to_read": "0"})


def test_rows_to_read_positive_allowed():
    parsed = parse_upload_form({"rows_to_read": "5"})
    assert parsed["rows_to_read"] == 5


def test_rows_to_read_absent_allowed():
    """rows_to_read is optional (allow_none) — absence reads all rows."""
    parsed = parse_upload_form({"table_name": "t"})
    assert "rows_to_read" not in parsed


@pytest.mark.parametrize("choice", ["fail", "replace", "append"])
def test_already_exists_valid_choices(choice):
    parsed = parse_upload_form({"already_exists": choice})
    assert parsed["already_exists"] == choice


def test_already_exists_invalid_rejected():
    """already_exists outside the OneOf set is rejected (upstream
    OneOf(("fail", "replace", "append")))."""
    with pytest.raises(CommandInvalidError, match="already_exists"):
        parse_upload_form({"already_exists": "garbage"})
