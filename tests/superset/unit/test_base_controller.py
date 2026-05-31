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
"""Unit tests for base controller utilities."""

from unittest.mock import MagicMock

import pytest

from superset.controllers.base import (
    _escape_like,
    extract_ids,
    extract_ids_required,
    extract_pagination,
    get_info_payload,
    serialize_list_response,
)
from superset.exceptions import SupersetValidationException


def test_escape_like_percent():
    """Test escaping of % character."""
    assert _escape_like("100%") == r"100\%"


def test_escape_like_underscore():
    """Test escaping of _ character."""
    assert _escape_like("a_b") == r"a\_b"


def test_escape_like_backslash():
    """Test escaping of backslash character."""
    assert _escape_like(r"a\b") == r"a\\b"


def test_escape_like_backslash_then_percent():
    """Test escaping of backslash followed by % (order matters)."""
    assert _escape_like(r"a\%b") == r"a\\\%b"


def test_escape_like_no_special_chars():
    """Test that strings without special characters are unchanged."""
    assert _escape_like("hello") == "hello"


def test_escape_like_multiple_specials():
    """Test escaping of multiple special characters in one string."""
    assert _escape_like(r"a\b_c%d") == r"a\\b\_c\%d"


# ---------------------------------------------------------------------------
# NEW-T12: extract_ids
# ---------------------------------------------------------------------------


def test_extract_ids_returns_ids():
    """extract_ids returns list of ints from rison params."""
    assert extract_ids({"ids": [1, 2, 3]}) == [1, 2, 3]


def test_extract_ids_empty_list():
    assert extract_ids({"ids": []}) == []


def test_extract_ids_none_params():
    assert extract_ids(None) == []


def test_extract_ids_missing_key():
    assert extract_ids({"page": 0}) == []


def test_extract_ids_non_list_raises():
    with pytest.raises(ValueError, match="ids must be a list"):
        extract_ids({"ids": "not-a-list"})


def test_extract_ids_non_int_element_raises():
    with pytest.raises(ValueError, match="Each id must be an integer"):
        extract_ids({"ids": [1, "two", 3]})


def test_extract_ids_required_raises_on_empty():
    with pytest.raises(SupersetValidationException, match="required"):
        extract_ids_required({"ids": []})


def test_extract_ids_required_returns_ids():
    assert extract_ids_required({"ids": [5, 10]}) == [5, 10]


# ---------------------------------------------------------------------------
# NEW-T12: serialize_list_response
# ---------------------------------------------------------------------------


def test_serialize_list_response_basic():
    item1 = MagicMock()
    item1.id = 1
    item1.name = "Alpha"
    item2 = MagicMock()
    item2.id = 2
    item2.name = "Beta"

    result = serialize_list_response([item1, item2], total=2, columns=["id", "name"])
    assert result["count"] == 2
    assert len(result["result"]) == 2
    assert result["result"][0] == {"id": 1, "name": "Alpha"}
    assert result["result"][1] == {"id": 2, "name": "Beta"}


def test_serialize_list_response_empty():
    result = serialize_list_response([], total=0, columns=["id"])
    assert result["count"] == 0
    assert result["result"] == []


def test_serialize_list_response_missing_attr():
    """Attributes not on the model return None."""
    item = MagicMock(spec=["id"])
    item.id = 1
    result = serialize_list_response([item], total=1, columns=["id", "missing"])
    assert result["result"][0]["id"] == 1
    assert result["result"][0]["missing"] is None


# ---------------------------------------------------------------------------
# NEW-T12: extract_pagination
# ---------------------------------------------------------------------------


def test_extract_pagination_defaults():
    page, page_size = extract_pagination(None)
    assert page == 0
    assert page_size == 25


def test_extract_pagination_custom():
    page, page_size = extract_pagination({"page": 3, "page_size": 50})
    assert page == 3
    assert page_size == 50


# ---------------------------------------------------------------------------
# NEW-T12: get_info_payload
# ---------------------------------------------------------------------------


async def test_get_info_payload_registered_spec():
    """get_info_payload serves the registered info_builder spec first.

    "Chart" has a descriptor in ``superset.info_builder.specs.RESOURCE_SPECS``,
    so the payload is assembled from the spec (populated add/edit columns)
    regardless of the dao — the SA-introspection fallback is NOT reached.
    """
    dao = MagicMock(spec=[])  # no model_cls — irrelevant for a registered spec
    result = await get_info_payload(dao, "Chart", ["can_read", "can_write"])
    assert result["permissions"] == ["can_read", "can_write"]
    # Populated from the registered Chart spec (not the empty SA fallback)
    assert len(result["add_columns"]) >= 1
    names = [c["name"] for c in result["add_columns"]]
    assert "slice_name" in names
    assert result["add_title"] == "Add Slice"


async def test_get_info_payload_sa_fallback():
    """get_info_payload falls back to SA introspection for unregistered models.

    "Database" has no info_builder descriptor, so ``build_info_payload``
    returns ``None`` and the function introspects the dao's ``model_cls``
    via ``sqlalchemy.inspect``.
    """
    from unittest.mock import patch

    # Mock a simple mapper with one column
    mock_col = MagicMock()
    mock_col.key = "database_name"
    mock_col.type = MagicMock(__str__=lambda self: "VARCHAR(250)")
    mock_col.nullable = False
    mock_col.unique = False

    mock_mapper = MagicMock()
    mock_mapper.columns = [mock_col]

    model_cls = MagicMock()
    dao = MagicMock()
    dao.model_cls = model_cls

    # Production imports ``from sqlalchemy import inspect as sa_inspect``
    # inside the function, so patching ``sqlalchemy.inspect`` intercepts it.
    with patch("sqlalchemy.inspect", return_value=mock_mapper):
        result = await get_info_payload(dao, "Database", ["can_read"])

    assert result["permissions"] == ["can_read"]
    assert len(result["add_columns"]) == 1
    assert result["add_columns"][0]["name"] == "database_name"
    assert result["add_columns"][0]["required"] is True
