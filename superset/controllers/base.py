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

import functools
import io
import logging
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from superset.exceptions import SupersetValidationException

logger = logging.getLogger(__name__)


def extract_pagination(rison_params: dict[str, Any] | None) -> tuple[int, int]:
    """Extract page and page_size from rison params."""
    page = (rison_params or {}).get("page", 0)
    page_size = (rison_params or {}).get("page_size", 25)
    return page, page_size


def extract_order(
    rison_params: dict[str, Any] | None,
) -> tuple[str | None, str]:
    """Extract order_column and order_direction from rison params."""
    params = rison_params or {}
    return params.get("order_column"), params.get("order_direction", "asc")


# Map computed/virtual column names to the real DB column they derive from.
# Matches Flask-AppBuilder's @renders() mappings in the original Superset.
_COMPUTED_ORDER_COLUMNS: dict[str, str] = {
    "changed_on_delta_humanized": "changed_on",
    "changed_on_utc": "changed_on",
    "changed_by_name": "changed_by_fk",
}


def build_order_by(
    model_cls: type[Any],
    order_column: str | None,
    order_direction: str = "asc",
) -> list[Any] | None:
    """Build SQLAlchemy order_by clauses from rison order params."""
    if not order_column:
        return None
    # Resolve computed columns to their underlying DB column
    resolved = _COMPUTED_ORDER_COLUMNS.get(order_column, order_column)
    col = getattr(model_cls, resolved, None)
    if col is None or not hasattr(col, "desc"):
        return None
    if order_direction == "desc":
        return [col.desc()]
    return [col.asc()]


@functools.lru_cache(maxsize=32)
def _get_model_columns(model_cls: type[Any] | None) -> dict[str, Any]:
    """Return a mapping of column key -> column for a SQLAlchemy model (cached)."""
    if model_cls is None:
        return {}
    from sqlalchemy import inspect as sa_inspect

    mapper: Any = sa_inspect(model_cls)
    return {col.key: col for col in mapper.columns}


@functools.lru_cache(maxsize=32)
def _get_model_relationships(model_cls: type[Any] | None) -> dict[str, Any]:
    """Return a mapping of relationship key -> relationship for a model (cached)."""
    if model_cls is None:
        return {}
    from sqlalchemy import inspect as sa_inspect

    mapper: Any = sa_inspect(model_cls)
    return {rel.key: rel for rel in mapper.relationships}


def _cast_pk(value: Any) -> Any:
    """Cast a RISON filter value to int if it looks like an integer PK.

    RISON encodes all values as strings.  asyncpg is strict about types
    and will refuse ``integer = varchar`` comparisons, so we convert
    string-encoded integers back to ``int`` before passing them to
    SQLAlchemy filters.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    return value


def build_rison_query_params(  # noqa: C901
    model_cls: type[Any],
    rison_params: dict[str, Any] | None,
    *,
    custom_filters: dict[str, Any] | None = None,
) -> tuple[list[Any], list[Any] | None, int, int]:
    """Parse Rison query parameters into filters, ordering, and pagination.

    Supports the following filter operators:
        eq, neq, sw (starts with), ew (ends with), ct (contains),
        nct (not contains), gt, lt, gte, lte,
        rel_m_m (many-to-many relationship), rel_o_m (many-to-one / FK).

    Custom filters (e.g. ``chart_is_favorite``, ``chart_is_certified``)
    are supported via the ``custom_filters`` parameter — a dict mapping
    ``opr`` names to callables ``(model_cls, value) -> SQLAlchemy clause``.

    Returns:
        A tuple of ``(filters, order_by, page, page_size)``.
    """

    from superset.utils import escape_like

    params = rison_params or {}
    page, page_size = extract_pagination(rison_params)

    # -- Validate column names via SQLAlchemy mapper inspection --
    valid_columns = _get_model_columns(model_cls)  # type: ignore[arg-type]
    valid_rels = _get_model_relationships(model_cls)  # type: ignore[arg-type]

    # -- Build filters --
    filters: list[Any] = []
    for flt in params.get("filters", []):
        col_name = flt.get("col")
        op = flt.get("opr")
        value = flt.get("value")

        # 1. Custom filters (chart_is_favorite, chart_is_certified, etc.)
        if custom_filters and op in custom_filters:
            clause = custom_filters[op](model_cls, value)
            if clause is not None:
                filters.append(clause)
            continue

        # 2. Relationship filters: rel_m_m and rel_o_m
        #    RISON passes IDs as strings (e.g. value:'1').  asyncpg is
        #    strict about types, so we must cast to int for integer PKs.
        if op == "rel_m_m" and col_name in valid_rels:
            rel = valid_rels[col_name]
            rel_model = rel.mapper.class_
            typed_value = _cast_pk(value)
            rel_attr = getattr(model_cls, col_name)
            filters.append(rel_attr.any(rel_model.id == typed_value))
            continue

        if op == "rel_o_m" and col_name in valid_rels:
            # For many-to-one relationships, filter by the FK column directly
            typed_value = _cast_pk(value)
            fk_col_name = f"{col_name}_fk"
            if hasattr(model_cls, fk_col_name):
                filters.append(getattr(model_cls, fk_col_name) == typed_value)
            else:
                # Fallback: try .has() for scalar relationships
                rel = valid_rels[col_name]
                rel_model = rel.mapper.class_
                rel_attr = getattr(model_cls, col_name)
                filters.append(rel_attr.has(rel_model.id == typed_value))
            continue

        # 3. Simple column filters
        if col_name not in valid_columns:
            continue
        col_attr = getattr(model_cls, col_name)
        if op == "eq":
            filters.append(col_attr == value)
        elif op == "neq":
            filters.append(col_attr != value)
        elif op == "sw":
            filters.append(col_attr.ilike(f"{escape_like(str(value))}%"))
        elif op == "ew":
            filters.append(col_attr.ilike(f"%{escape_like(str(value))}"))
        elif op == "ct":
            filters.append(col_attr.ilike(f"%{escape_like(str(value))}%"))
        elif op == "nct":
            filters.append(~col_attr.ilike(f"%{escape_like(str(value))}%"))
        elif op == "gt":
            filters.append(col_attr > value)
        elif op == "lt":
            filters.append(col_attr < value)
        elif op == "gte":
            filters.append(col_attr >= value)
        elif op == "lte":
            filters.append(col_attr <= value)

    # -- Build order_by --
    order_by = build_order_by(
        model_cls,
        params.get("order_column"),
        params.get("order_direction", "asc"),
    )

    return filters, order_by, page, page_size


def extract_ids(rison_params: list[int] | dict[str, Any] | None) -> list[int]:
    """Extract and validate ``ids`` from Rison query parameters.

    Supports two formats used by the frontend:
    - Array of ints directly: ``!(1,2,3)`` → ``[1, 2, 3]``
    - Dict with ``ids`` key: ``(ids:!(1,2,3))`` → ``{"ids": [1, 2, 3]}``

    Returns a list of integers.  Raises ``ValueError`` when elements
    are not integers so the caller can return a 422 response.
    """
    if rison_params is None:
        return []
    if isinstance(rison_params, list):
        raw = rison_params
    else:
        raw = rison_params.get("ids", [])
    if not isinstance(raw, list):
        raise ValueError("ids must be a list")
    ids: list[int] = []
    for item in raw:
        if not isinstance(item, int):
            raise ValueError(f"Each id must be an integer, got {type(item).__name__}")
        ids.append(item)
    return ids


def extract_ids_required(rison_params: list[int] | dict[str, Any] | None) -> list[int]:
    """Extract IDs from Rison params, raising SupersetValidationException if empty."""
    ids = extract_ids(rison_params)
    if not ids:
        raise SupersetValidationException(
            "ids parameter is required and cannot be empty"
        )
    return ids


async def stream_zip(buf: io.BytesIO) -> AsyncGenerator[bytes, None]:
    """Stream a ZIP BytesIO in 8 KiB chunks to avoid loading entire file in RAM."""
    buf.seek(0)
    while chunk := buf.read(8192):
        yield chunk


def build_export_headers(
    filename: str,
    token: str | None = None,
) -> dict[str, str]:
    """Build response headers for ZIP export endpoints.

    Includes ``Content-Disposition`` and, when a ``token`` query parameter
    is present, a ``Set-Cookie`` header that sets ``token=done`` so the
    frontend can track download completion.
    """
    headers: dict[str, str] = {
        "Content-Disposition": f"attachment; filename={filename}",
    }
    if token:
        headers["Set-Cookie"] = "token=done; Path=/; SameSite=Lax"
    return headers


def _escape_like(value: str) -> str:
    """Escape LIKE special characters (\\, %, _) to prevent wildcard injection.

    .. deprecated::
        Use :func:`superset.utils.escape_like` instead. This wrapper exists
        for backward compatibility.
    """
    from superset.utils import escape_like

    return escape_like(value)


def _resolve_attr(obj: Any, path: str) -> Any:
    """Resolve a dotted attribute path like ``owners.id`` or ``database.database_name``.

    For relationship collections (lists), returns a list of the nested attribute.
    For scalar relationships, returns the nested attribute directly.
    """
    parts = path.split(".", 1)
    val = getattr(obj, parts[0], None)
    if len(parts) == 1:
        return val
    nested = parts[1]
    if isinstance(val, list):
        return [getattr(item, nested, None) for item in val]
    if val is None:
        return None
    return getattr(val, nested, None)


def _serialize_item(item: Any, columns: list[str]) -> dict[str, Any]:
    """Serialize a single model item, handling nested paths.

    Nested paths (``relationship.field``) are grouped into sub-dicts or
    lists-of-dicts matching the Flask-AppBuilder API format.
    """
    result: dict[str, Any] = {}
    # Group nested columns by their relationship name
    nested_groups: dict[str, list[str]] = {}
    for col in columns:
        if "." in col:
            rel, field = col.split(".", 1)
            nested_groups.setdefault(rel, []).append(field)
        else:
            result[col] = getattr(item, col, None)

    for rel, fields in nested_groups.items():
        rel_val = getattr(item, rel, None)
        if rel_val is None:
            result[rel] = None
        elif isinstance(rel_val, list):
            result[rel] = [{f: getattr(r, f, None) for f in fields} for r in rel_val]
        else:
            result[rel] = {f: getattr(rel_val, f, None) for f in fields}

    return result


def _prettify_column(name: str) -> str:
    """Convert a column name to a human-readable label.

    Matches Flask-AppBuilder's ``_prettify_column`` logic:
    ``cache_timeout`` → ``Cache Timeout``,
    ``changed_by.first_name`` → ``Changed By First Name``.
    """
    return name.replace(".", " ").replace("_", " ").title()


def serialize_list_response(
    items: list[Any],
    total: int,
    columns: list[str],
    *,
    list_title: str = "",
    order_columns: list[str] | None = None,
    label_columns: dict[str, str] | None = None,
    description_columns: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Serialize a list of models into ApiListResponse-compatible dict.

    Supports dotted paths like ``owners.id``, ``database.database_name``.
    Nested relationships are grouped into sub-objects matching the
    Flask-AppBuilder REST API format the frontend expects.

    Automatically post-processes each item:
    - ``uuid`` fields are converted to strings
    - ``changed_on``/``created_on`` datetimes are left as-is for further
      processing or JSON serialization
    """
    result = [_serialize_item(item, columns) for item in (items or [])]
    ids: list[str] = []
    for row in result:
        # uuid → string
        if "uuid" in row and row["uuid"] is not None:
            row["uuid"] = str(row["uuid"])
        # ``ids`` is declared as array of strings in the original
        # Superset OpenAPI spec (FAB ApiListResponse); cast pks to str
        # so contract validators don't reject integer entries.
        row_id = row.get("id") if "id" in row else getattr(row, "id", None)
        if row_id is not None:
            ids.append(str(row_id))

    # Auto-generate label_columns if not provided
    if label_columns is None:
        label_columns = {col: _prettify_column(col) for col in columns}

    response: dict[str, Any] = {
        "count": total,
        "description_columns": description_columns or {},
        "ids": ids,
        "label_columns": label_columns,
        "list_columns": list(columns),
        "list_title": list_title,
        "result": result,
    }
    if order_columns is not None:
        response["order_columns"] = order_columns
    return response


async def get_info_payload(
    dao: Any,
    model_name: str,
    permissions: list[str],
) -> dict[str, Any]:
    """Build the ``GET /<resource>/_info`` response.

    Resources registered in :mod:`superset.info_builder.specs` get a
    dynamically-assembled Marshmallow-style payload byte-equivalent to
    the original Apache Superset response (``add_title``,
    ``add_columns`` / ``edit_columns`` with ``type: 'String'`` and
    validators, full filter operator catalogue). Filter operators are
    derived live from the SA model; Marshmallow-specific bits live in
    :mod:`superset.info_builder.specs`.

    Resources without a descriptor fall back to a minimal
    SA-introspected payload — useful for rarely-used legacy endpoints.
    """
    from superset.info_builder.builder import build_info_payload

    payload = build_info_payload(model_name, permissions=permissions)
    if payload is not None:
        return payload

    # Fallback — SA introspection. Used by resources we haven't shipped
    # a static fixture for; keeps the endpoint usable while the
    # frontend keys off the fixture for the major models.
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


_EXTRA_FIELDS_REL: dict[str, list[str]] = {
    # Only ``owners`` carries extra fields in original Superset —
    # see ``BaseSupersetModelRestApi.extra_fields_rel_fields`` at
    # superset_old/views/base_api.py:327. ``created_by``/``changed_by``
    # responses keep ``extra: {}`` for contract parity.
    "owners": ["email", "active"],
}


async def get_related_payload(  # noqa: C901
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
            from sqlalchemy import or_

            like_value = f"%{_escape_like(filter_value)}%"

            # For User-like models (with first_name + last_name), apply
            # combined name search matching FilterRelatedOwners behavior
            if hasattr(rel_model, "first_name") and hasattr(rel_model, "last_name"):
                combined = rel_model.first_name + " " + rel_model.last_name
                or_clauses = [combined.ilike(like_value)]
                if hasattr(rel_model, "username"):
                    or_clauses.append(rel_model.username.ilike(like_value))
                stmt = stmt.where(or_(*or_clauses))
            else:
                # Try common name columns for non-User models
                for name_col in (
                    "name",
                    "username",
                    "database_name",
                    "table_name",
                    "label",
                ):
                    if hasattr(rel_model, name_col):
                        stmt = stmt.where(
                            getattr(rel_model, name_col).ilike(like_value)
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

        extra_fields = _EXTRA_FIELDS_REL.get(column_name, [])

        def _build_item(item: Any) -> dict[str, Any]:
            # Always emit an ``extra`` key (empty dict when the related
            # column has no extra fields) — original Superset's
            # RelatedResultResponseSchema declares ``extra`` as a
            # required object, and contract tests rely on its presence.
            entry: dict[str, Any] = {
                "value": item.id,
                "text": str(item),
                "extra": {field: getattr(item, field, None) for field in extra_fields}
                if extra_fields
                else {},
            }
            return entry

        return {
            "count": total or 0,
            "result": [_build_item(item) for item in items],
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
        # Match original ``DistinctFilter`` behaviour — drop NULL values
        # so ``{"text": "None", "value": null}`` doesn't pollute the
        # filter dropdown (and contract snapshots).
        base_stmt = sa_select(func.distinct(col)).where(col.is_not(None))

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
        values = [v for v in result.scalars().all() if v is not None]

        return {
            "count": total or 0,
            "result": [{"text": str(v), "value": v} for v in values],
        }
    except (SQLAlchemyError, AttributeError, ValueError):
        logger.warning("Failed to get distinct '%s'", column_name, exc_info=True)
        return {"count": 0, "result": []}
