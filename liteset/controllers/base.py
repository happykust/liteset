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
"""Base controller mixin providing _info, /related/, /distinct/ endpoints.

Flask's BaseSupersetModelRestApi auto-generates these endpoints. This mixin
provides async equivalents for Litestar controllers that need them.

Usage:
    class ChartController(CRUDMixin, Controller):
        path = "/api/v1/chart"
        _model_name = "Chart"
        _read_permission = "can_read"
        ...

The mixin methods (`info`, `related`, `distinct`) are standalone helper
functions rather than true Litestar route handlers, because Litestar
Controllers inherit routes via class hierarchy — and we want each
concrete controller to register them explicitly at its own path prefix.

Concrete controllers call these helpers from their own `@get` handlers.
"""

from __future__ import annotations

import io
import logging
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from liteset.exceptions import LitesetValidationException

logger = logging.getLogger(__name__)


def extract_pagination(rison_params: dict[str, Any] | None) -> tuple[int, int]:
    """Extract page and page_size from rison params."""
    page = (rison_params or {}).get("page", 0)
    page_size = (rison_params or {}).get("page_size", 25)
    return page, page_size


def extract_ids(rison_params: dict[str, Any] | None) -> list[int]:
    """Extract and validate ``ids`` from Rison query parameters.

    Returns a list of integers.  Raises ``ValueError`` when elements
    are not integers so the caller can return a 422 response.
    """
    raw = (rison_params or {}).get("ids", [])
    if not isinstance(raw, list):
        raise ValueError("ids must be a list")
    ids: list[int] = []
    for item in raw:
        if not isinstance(item, int):
            raise ValueError(f"Each id must be an integer, got {type(item).__name__}")
        ids.append(item)
    return ids


def extract_ids_required(rison_params: dict[str, Any] | None) -> list[int]:
    """Extract IDs from Rison params, raising LitesetValidationException if empty."""
    ids = extract_ids(rison_params)
    if not ids:
        raise LitesetValidationException(
            "ids parameter is required and cannot be empty"
        )
    return ids


async def stream_zip(buf: io.BytesIO) -> AsyncGenerator[bytes, None]:
    """Stream a ZIP BytesIO in 8 KiB chunks to avoid loading entire file in RAM."""
    buf.seek(0)
    while chunk := buf.read(8192):
        yield chunk


def _escape_like(value: str) -> str:
    """Escape LIKE special characters (\\, %, _) to prevent wildcard injection."""
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def serialize_list_response(
    items: list[Any],
    total: int,
    columns: list[str],
) -> dict[str, Any]:
    """Serialize a list of models into ApiListResponse-compatible dict.

    Args:
        items: Model instances from DAO.find_all()
        total: Total count from DAO.count()
        columns: Attribute names to include in each result item
    """
    return {
        "result": [
            {col: getattr(item, col, None) for col in columns} for item in (items or [])
        ],
        "count": total,
    }


async def get_info_payload(
    dao: Any,
    model_name: str,
    permissions: list[str],
) -> dict[str, Any]:
    """Build _info response with permissions, column list, and filter metadata.

    This matches Flask's GET /_info response used by the frontend to build
    filter UIs, permission checks, and form field lists.
    """
    # Introspect model columns from DAO
    model_cls = getattr(dao, "model_cls", None)
    columns: list[dict[str, Any]] = []
    if model_cls is not None:
        try:
            from sqlalchemy import inspect as sa_inspect

            mapper = sa_inspect(model_cls)
            columns = [
                {
                    "name": col.key,
                    "label": col.key.replace("_", " ").title(),
                    "description": "",
                    "type": str(col.type) if hasattr(col, "type") else "unknown",
                    "nullable": getattr(col, "nullable", True),
                    "required": not getattr(col, "nullable", True),
                    "unique": getattr(col, "unique", False),
                }
                for col in mapper.columns
            ]
        except Exception:  # noqa: BLE001
            logger.debug("Could not introspect model %s columns", model_cls)

    return {
        "permissions": permissions,
        "add_columns": columns,
        "edit_columns": columns,
        "label_columns": {col["name"]: col["label"] for col in columns},
        "filters": {
            col["name"]: [
                {"name": "sw", "operator": "sw"},
                {"name": "eq", "operator": "eq"},
                {"name": "neq", "operator": "neq"},
                {"name": "ct", "operator": "ct"},
            ]
            for col in columns
            if col.get("type", "")
            .upper()
            .startswith(("VARCHAR", "TEXT", "STRING", "CHAR"))
        },
    }


async def get_related_payload(
    dao: Any,
    column_name: str,
    rison_params: dict[str, Any] | None = None,
    *,
    allowed_fields: frozenset[str] | None = None,
    base_filters: list[Any] | None = None,
    page: int = 0,
    page_size: int = 25,
    filter_value: str = "",
) -> dict[str, Any]:
    """Build /related/{column_name} response for select dropdowns.

    Returns distinct values for a relationship column (e.g., owners, databases).
    Used by the frontend to populate select inputs in forms.

    Args:
        allowed_fields: If provided, only these column names are permitted.
            Returns 404-style empty result for disallowed names, matching
            Superset's ``allowed_rel_fields`` behavior.
    """
    if allowed_fields is not None and column_name not in allowed_fields:
        return {"count": 0, "result": []}

    if rison_params is not None:
        page, page_size = extract_pagination(rison_params)
        filter_value = rison_params.get("filter", "") or filter_value

    include_ids = (rison_params or {}).get("include_ids", [])

    model_cls = getattr(dao, "model_cls", None)
    if model_cls is None:
        return {"count": 0, "result": []}

    try:
        from sqlalchemy import func, inspect as sa_inspect, select as sa_select

        mapper = sa_inspect(model_cls)
        if column_name not in mapper.relationships:
            return {"count": 0, "result": []}

        rel = mapper.relationships[column_name]
        rel_model = rel.mapper.class_
        stmt = sa_select(rel_model)

        # Apply base_filters if provided
        if base_filters:
            for bf in base_filters:
                stmt = stmt.where(bf)

        # Apply text filter if provided
        if filter_value:
            # Try common name columns
            for name_col in (
                "name",
                "username",
                "database_name",
                "table_name",
                "label",
            ):
                if hasattr(rel_model, name_col):
                    stmt = stmt.where(
                        getattr(rel_model, name_col).ilike(
                            f"%{_escape_like(filter_value)}%"
                        )
                    )
                    break

        total = await dao.session.scalar(
            sa_select(func.count()).select_from(stmt.subquery())
        )
        stmt = stmt.offset(page * page_size).limit(page_size)
        result = await dao.session.execute(stmt)
        items = list(result.scalars().all())

        if include_ids:
            if page > 0:
                # Cannot combine pagination with include_ids
                pass
            else:
                original_count = len(items)
                include_stmt = sa_select(rel_model).where(rel_model.id.in_(include_ids))
                include_result = await dao.session.execute(include_stmt)
                include_items = include_result.scalars().all()
                existing_ids = {item.id for item in items}
                for item in include_items:
                    if item.id not in existing_ids:
                        items.append(item)
                # Adjust total to account for injected items
                extra_count = len(items) - original_count
                total = (total or 0) + extra_count

        return {
            "count": total or 0,
            "result": [
                {
                    "value": item.id,
                    "text": str(item),
                }
                for item in items
            ],
        }
    except (SQLAlchemyError, AttributeError, ValueError):
        logger.warning("Failed to resolve related '%s'", column_name, exc_info=True)
        return {"count": 0, "result": []}


async def get_distinct_payload(
    dao: Any,
    column_name: str,
    rison_params: dict[str, Any] | None = None,
    *,
    allowed_fields: frozenset[str] | None = None,
    base_filters: list[Any] | None = None,
    page: int = 0,
    page_size: int = 25,
    filter_value: str = "",
) -> dict[str, Any]:
    """Build /distinct/{column_name} response for filter dropdowns.

    Returns distinct values for a column. Used by frontend filter UIs.

    Args:
        allowed_fields: If provided, only these column names are permitted.
            Returns 404-style empty result for disallowed names, matching
            Superset's ``allowed_distinct_fields`` behavior.
    """
    if allowed_fields is not None and column_name not in allowed_fields:
        return {"count": 0, "result": []}

    if rison_params is not None:
        page, page_size = extract_pagination(rison_params)
        filter_value = rison_params.get("filter", "") or filter_value

    model_cls = getattr(dao, "model_cls", None)
    if model_cls is None or not hasattr(model_cls, column_name):
        return {"count": 0, "result": []}

    try:
        from sqlalchemy import func, select as sa_select

        col = getattr(model_cls, column_name)
        base_stmt = sa_select(func.distinct(col))

        # Apply base_filters if provided
        if base_filters:
            for bf in base_filters:
                base_stmt = base_stmt.where(bf)

        if filter_value:
            base_stmt = base_stmt.where(col.ilike(f"%{_escape_like(filter_value)}%"))

        total = await dao.session.scalar(
            sa_select(func.count()).select_from(base_stmt.subquery())
        )
        stmt = base_stmt.offset(page * page_size).limit(page_size)
        result = await dao.session.execute(stmt)
        values = result.scalars().all()

        return {
            "count": total or 0,
            "result": [{"text": str(v), "value": v} for v in values],
        }
    except (SQLAlchemyError, AttributeError, ValueError):
        logger.warning("Failed to get distinct '%s'", column_name, exc_info=True)
        return {"count": 0, "result": []}
