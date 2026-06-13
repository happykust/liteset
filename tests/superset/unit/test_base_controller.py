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

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.controllers.base import (
    _escape_like,
    extract_ids,
    extract_ids_required,
    extract_pagination,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
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


def test_serialize_list_response_ids_populated_when_id_not_in_columns():
    """ids array is populated from ORM items even when 'id' is absent from columns.

    1:1 with FAB BaseModelRestApi.get_list which calls self.datamodel.get_keys(lst)
    — getattr(item, pk_name) on each ORM object, completely independent of
    list_columns. The Log endpoint (superset_old/views/log/api.py:48-60) omits
    "id" from list_columns yet the response carries populated ids.
    """
    item1 = MagicMock()
    item1.id = 7
    item1.action = "mount_dashboard"
    item1.user_id = 3
    item2 = MagicMock()
    item2.id = 8
    item2.action = "mount_explorer"
    item2.user_id = 4

    # "id" intentionally absent — mirrors Log list_columns
    columns = ["action", "user_id"]
    result = serialize_list_response([item1, item2], total=2, columns=columns)

    # ids must be populated from the ORM items, not from the row dict
    assert result["ids"] == ["7", "8"], (
        "ids should be populated from ORM item.id even when 'id' is absent from columns"
    )
    # list_columns in the response must NOT include "id" (matches original list_columns)
    assert "id" not in result["list_columns"]
    # result rows must NOT contain an id key
    assert "id" not in result["result"][0]
    assert "id" not in result["result"][1]


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


# ---------------------------------------------------------------------------
# get_related_payload: 404 when column_name passes allowed_fields but has no
# SA relationship — mirrors superset_old/views/base_api.py:585-588.
# ---------------------------------------------------------------------------


async def test_get_related_payload_unknown_relationship_raises_404():
    """column_name passes allowed_fields but is absent from mapper.relationships
    → NotFoundException (HTTP 404), not HTTP 200 with empty payload.

    1:1 with superset_old/views/base_api.py:585-588:
        try:
            datamodel = self.datamodel.get_related_interface(column_name)
        except KeyError:
            return self.response_404()
    """
    from unittest.mock import patch

    from litestar.exceptions import NotFoundException

    model_cls = MagicMock()
    dao = MagicMock()
    dao.model_cls = model_cls

    # Mapper has no relationships for this name
    mock_mapper = MagicMock()
    mock_mapper.relationships = {}

    # allowed_fields contains the name (passes the first guard), but the SA
    # mapper has no matching relationship — the second 404 path must fire.
    with patch("sqlalchemy.inspect", return_value=mock_mapper):
        with pytest.raises(NotFoundException):
            await get_related_payload(
                dao,
                "nonexistent_rel",
                allowed_fields=frozenset({"nonexistent_rel"}),
            )


async def test_get_distinct_payload_preserves_raw_value_type():
    """/distinct ``text`` must preserve the raw column value type (not str()),
    1:1 with upstream views/base_api.py which uses ``item[0]`` for both
    ``text`` and ``value`` — e.g. an int column yields {"text": 5, ...}."""
    from superset.models.sql_lab import SavedQuery

    dao = MagicMock()
    dao.model_cls = SavedQuery  # real model so the column ops build cleanly
    dao.session = MagicMock()
    dao.session.scalar = AsyncMock(return_value=2)
    result_obj = MagicMock()
    result_obj.scalars.return_value.all.return_value = [5, 6]
    dao.session.execute = AsyncMock(return_value=result_obj)

    out = await get_distinct_payload(dao, "id")

    assert out["result"] == [
        {"text": 5, "value": 5},
        {"text": 6, "value": 6},
    ]
    assert isinstance(out["result"][0]["text"], int)


def test_info_payload_advertises_id_uuid_custom_filters():
    """The _info payload must advertise the favorite/certified/owned filter
    operators that key off ``id`` (and the ``uuid`` ops) — they were dropped
    when ``id``/``uuid`` were absent from search_columns, so clients reading
    filters['id'] to build the filter UI lost those toggles."""
    from superset.info_builder.builder import build_info_payload

    chart = build_info_payload("Chart")
    chart_id_ops = {f["operator"] for f in chart["filters"].get("id", [])}
    assert {
        "chart_is_favorite",
        "chart_is_certified",
        "chart_owned_created_favored_by_me",
    } <= chart_id_ops
    assert "uuid" in chart["filters"]

    dash = build_info_payload("Dashboard")
    dash_id_ops = {f["operator"] for f in dash["filters"].get("id", [])}
    assert {"dashboard_is_favorite", "dashboard_is_certified"} <= dash_id_ops

    ds = build_info_payload("SqlaTable")
    ds_id_ops = {f["operator"] for f in ds["filters"].get("id", [])}
    assert "dataset_is_certified" in ds_id_ops

    sq = build_info_payload("SavedQuery")
    sq_id_ops = {f["operator"] for f in sq["filters"].get("id", [])}
    assert "saved_query_is_fav" in sq_id_ops
