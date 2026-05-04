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
"""File-upload form helpers for ``POST /api/v1/database/{pk}/upload/``.

Mirrors ``superset_old/databases/schemas.py:UploadPostSchema`` field-by-field
so the controller in ``superset/controllers/database.py`` can build the
same shape of options dict that :class:`CSVReader` / :class:`ExcelReader`
/ :class:`ColumnarReader` expect.

The original Superset schema is a Marshmallow ``Schema``; we don't ship
Marshmallow in liteset, so the fields are parsed off the
``multipart/form-data`` payload directly inside :func:`parse_upload_form`.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


__all__ = [
    "ALLOWED_UPLOAD_EXTENSIONS",
    "build_reader_options",
    "parse_delimited_list",
    "parse_upload_form",
    "validate_file_extension",
]


# Default extension allow-list — mirrors the Superset config key
# ``ALLOWED_EXTENSIONS`` from ``superset_old/config.py``.  When the
# settings module exposes a custom list we honour it; otherwise we use
# this conservative default.
ALLOWED_UPLOAD_EXTENSIONS: frozenset[str] = frozenset(
    {"csv", "tsv", "xls", "xlsx", "parquet", "zip"}
)


def parse_delimited_list(value: Any) -> list[str] | None:
    """Parse a comma-delimited string into a list of stripped tokens.

    Mirrors ``superset_old/databases/schemas.py:DelimitedListField``.
    Empty strings or falsy inputs return ``None`` so callers can
    distinguish "not provided" from "empty".
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or None


def _coerce_bool(value: Any) -> bool | None:
    """Coerce a form-encoded boolean into ``True`` / ``False`` / ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if not lowered:
            return None
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return None


def _coerce_int(value: Any) -> int | None:
    """Coerce a form-encoded integer; return ``None`` on failure."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_file_extension(
    filename: str,
    *,
    allowed: Optional[Iterable[str]] = None,
) -> bool:
    """Return ``True`` iff ``filename``'s extension is in ``allowed``.

    Mirrors ``BaseUploadFilePostSchemaMixin.validate_file_extension``
    (case-insensitive comparison on the trailing ``.ext`` token).
    """
    if not filename:
        return False
    if "." not in filename:
        return False
    suffix = filename.rsplit(".", 1)[1].lower()
    allowed_set = (
        {ext.lower() for ext in allowed}
        if allowed is not None
        else ALLOWED_UPLOAD_EXTENSIONS
    )
    return suffix in allowed_set


# ---------------------------------------------------------------------------
# Form-field set: mirrors ``UploadPostSchema`` 1:1
# ---------------------------------------------------------------------------

#: All form fields accepted by ``POST /database/<pk>/upload/``.
#: Sourced from ``superset_old/databases/schemas.py:UploadPostSchema``.
UPLOAD_FORM_FIELDS: frozenset[str] = frozenset(
    {
        # Common
        "type",
        "table_name",
        "schema",
        "already_exists",
        "index_label",
        "index_column",
        "dataframe_index",
        "columns_read",
        # CSV-only
        "delimiter",
        "column_data_types",
        "day_first",
        "skip_blank_lines",
        "skip_initial_space",
        # CSV + Excel
        "column_dates",
        "decimal_character",
        "header_row",
        "null_values",
        "rows_to_read",
        "skip_rows",
        # Excel-only
        "sheet_name",
    }
)


def parse_upload_form(form: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    """Translate raw form values into the ``UploadPostSchema``-shaped dict.

    The result mirrors :class:`UploadPostSchema` after Marshmallow's
    ``post_load``: numeric fields are coerced, delimited lists are
    split, and ``column_data_types`` (a JSON string) is parsed into a
    dict.

    Raises:
        ValueError: when ``column_data_types`` is malformed JSON.
    """
    parsed: dict[str, Any] = {}

    # --- straight string passthroughs --------------------------------------
    for key in (
        "type",
        "table_name",
        "schema",
        "already_exists",
        "index_label",
        "index_column",
        "delimiter",
        "decimal_character",
        "sheet_name",
    ):
        value = form.get(key)
        if value not in (None, ""):
            parsed[key] = str(value)

    # --- bools -------------------------------------------------------------
    for key in ("dataframe_index", "day_first", "skip_blank_lines", "skip_initial_space"):
        coerced = _coerce_bool(form.get(key))
        if coerced is not None:
            parsed[key] = coerced

    # --- ints --------------------------------------------------------------
    for key in ("header_row", "rows_to_read", "skip_rows"):
        coerced = _coerce_int(form.get(key))
        if coerced is not None:
            parsed[key] = coerced

    # --- delimited lists ---------------------------------------------------
    for key in ("columns_read", "column_dates", "null_values"):
        coerced_list = parse_delimited_list(form.get(key))
        if coerced_list is not None:
            parsed[key] = coerced_list

    # --- column_data_types (JSON-encoded dict) -----------------------------
    raw_cdt = form.get("column_data_types")
    if raw_cdt not in (None, ""):
        if isinstance(raw_cdt, dict):
            parsed["column_data_types"] = raw_cdt
        else:
            try:
                parsed["column_data_types"] = _json.loads(raw_cdt)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Invalid JSON format for column_data_types"
                ) from exc

    return parsed


def build_reader_options(parsed_form: dict[str, Any]) -> dict[str, Any]:
    """Project the parsed form into the kwargs the readers expect.

    The reader options mirror the original ``CSVReaderOptions`` /
    ``ExcelReaderOptions`` / ``ColumnarReaderOptions`` typed dicts.
    Keys that the reader ignores are still safe to include.
    """
    # We just strip ``type``, ``table_name``, and ``schema`` — those are
    # consumed at the controller level.  Everything else is fair game.
    return {
        k: v
        for k, v in parsed_form.items()
        if k not in ("type", "table_name", "schema")
    }
