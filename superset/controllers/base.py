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

The upstream BaseSupersetModelRestApi auto-generates these endpoints. This mixin
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
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from litestar.connection import Request
from litestar.datastructures import UploadFile
from litestar.exceptions import ValidationException
from sqlalchemy.exc import SQLAlchemyError

from superset.exceptions import CommandInvalidError, SupersetValidationException

logger = logging.getLogger(__name__)


async def parse_import_request(
    request: Request[Any, Any, Any],
) -> tuple[
    io.BytesIO,
    str,
    bool,
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    """Extract the import bundle + options from a multipart import request.

    Replaces the ``data: UploadFile = Body(MULTI_PART)`` parameter injection
    used by the ``/import/`` endpoints. That injection crashed with
    ``StopIteration`` -> ``RuntimeError`` -> HTTP 500 when the request carried
    no file field, because Litestar's multipart extractor does
    ``next(v for v in form.values() if isinstance(v, UploadFile))``. Reading the
    form here lets a missing upload be a clean 4xx ``CommandInvalidError``.

    Returns ``(contents, filename, overwrite, passwords, ssh_tunnel_passwords,
    ssh_tunnel_private_keys, ssh_tunnel_private_key_passwords)`` — the JSON
    option fields decoded to dicts.
    ``filename`` is needed by the dashboard importer's ZIP-vs-JSON dispatch.
    """
    form = await request.form()
    upload = next((v for v in form.values() if isinstance(v, UploadFile)), None)
    if upload is None:
        # ValidationException is mapped to HTTP 400 by ``validation_error_handler``.
        raise ValidationException("No file uploaded for import")
    contents = io.BytesIO(await upload.read())
    filename = getattr(upload, "filename", None) or "import.zip"
    overwrite = str(form.get("overwrite", "")).strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )

    def _json_field(name: str) -> dict[str, str]:
        raw = form.get(name)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise CommandInvalidError(f"Invalid JSON in '{name}' field") from exc

    return (
        contents,
        filename,
        overwrite,
        _json_field("passwords"),
        _json_field("ssh_tunnel_passwords"),
        _json_field("ssh_tunnel_private_keys"),
        _json_field("ssh_tunnel_private_key_passwords"),
    )


def extract_pagination(
    rison_params: dict[str, Any] | None,
    default_page_size: int = 25,
) -> tuple[int, int]:
    """Extract page and page_size from rison params.

    :param rison_params: Rison-decoded query parameter dict (may be None).
    :param default_page_size: Page size to use when the caller does not
        provide ``page_size``.  Defaults to 25 for most endpoints; pass
        ``10`` for endpoints where the original Superset used a 10-row
        default (e.g. ``/security/roles/search/``).
    """
    page = (rison_params or {}).get("page", 0)
    page_size = (rison_params or {}).get("page_size", default_page_size)
    return page, page_size


def extract_order(
    rison_params: dict[str, Any] | None,
) -> tuple[str | None, str]:
    """Extract order_column and order_direction from rison params."""
    params = rison_params or {}
    return params.get("order_column"), params.get("order_direction", "asc")


# Map computed/virtual column names to the real DB column they derive from.
# Matches the upstream @renders() mappings in the original Superset.
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
    """Build SQLAlchemy order_by clauses from rison order params.

    ``InstrumentedAttribute`` for a *relationship* (``Slice.owners``,
    ``Slice.tags``, ``Slice.changed_by``) advertises ``.desc()`` /
    ``.asc()`` but those raise ``NotImplementedError`` when the query
    compiles — sorting by a relationship makes no SQL sense without a
    join. Filter to ColumnProperty-backed attributes only; unknown
    or relationship columns return ``None`` so the caller falls back
    to the default (PK tiebreak) order. Matches the upstream behavior where
    sorts referencing a non-column field silently no-op.
    """
    if not order_column:
        return None
    resolved = _COMPUTED_ORDER_COLUMNS.get(order_column, order_column)
    col = getattr(model_cls, resolved, None)
    if col is None:
        return None
    col_prop = getattr(col, "property", None)
    if col_prop is None or getattr(col_prop, "columns", None) is None:
        # ``owners``/``tags``/``changed_by`` etc. — relationship, not a
        # column. Sorting needs a join the caller didn't ask for.
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
    default_page_size: int = 25,
) -> tuple[list[Any], list[Any] | None, int, int]:
    """Parse Rison query parameters into filters, ordering, and pagination.

    Supports the following filter operators:
        eq, neq, sw (starts with), ew (ends with), ct (contains),
        nct (not contains), gt, lt, gte, lte,
        rel_m_m (many-to-many relationship), rel_o_m (many-to-one / FK).

    Custom filters (e.g. ``chart_is_favorite``, ``chart_is_certified``)
    are supported via the ``custom_filters`` parameter — a dict mapping
    ``opr`` names to callables ``(model_cls, value) -> SQLAlchemy clause``.

    :param default_page_size: Default page size when the client does not
        supply ``page_size``.  Matches the upstream ``ModelRestApi.page_size``
        default (20) when the original endpoint did not override it.

    Returns:
        A tuple of ``(filters, order_by, page, page_size)``.
    """

    from superset.utils import escape_like

    params = rison_params or {}
    page, page_size = extract_pagination(
        rison_params, default_page_size=default_page_size
    )

    valid_columns = _get_model_columns(model_cls)  # type: ignore[arg-type]
    valid_rels = _get_model_relationships(model_cls)  # type: ignore[arg-type]

    filters: list[Any] = []
    for flt in params.get("filters", []):
        col_name = flt.get("col")
        op = flt.get("opr")
        value = flt.get("value")

        if custom_filters and op in custom_filters:
            clause = custom_filters[op](model_cls, value)
            if clause is not None:
                filters.append(clause)
            continue

        # RISON passes IDs as strings (e.g. value:'1').  asyncpg is
        # strict about types, so we must cast to int for integer PKs.
        if op in ("rel_m_m", "nrel_m_m") and col_name in valid_rels:
            rel = valid_rels[col_name]
            rel_model = rel.mapper.class_
            typed_value = _cast_pk(value)
            rel_attr = getattr(model_cls, col_name)
            clause = rel_attr.any(rel_model.id == typed_value)
            # The upstream FilterRelationManyToMany has no negated arg_name, but
            # symmetry with nrel_o_m is useful and harmless; keep parity by
            # supporting it explicitly.
            filters.append(~clause if op == "nrel_m_m" else clause)
            continue

        if op in ("rel_o_m", "nrel_o_m") and col_name in valid_rels:
            # Many-to-one: filter by the FK column directly when present.
            # ``nrel_o_m`` (upstream FilterRelationOneToManyNotEqual,
            # models/sqla/filters.py:238) negates it.
            typed_value = _cast_pk(value)
            fk_col_name = f"{col_name}_fk"
            if hasattr(model_cls, fk_col_name):
                fk_col = getattr(model_cls, fk_col_name)
                filters.append(
                    fk_col != typed_value if op == "nrel_o_m" else fk_col == typed_value
                )
            else:
                # Fallback: try .has() for scalar relationships
                rel = valid_rels[col_name]
                rel_model = rel.mapper.class_
                rel_attr = getattr(model_cls, col_name)
                has_clause = rel_attr.has(rel_model.id == typed_value)
                filters.append(~has_clause if op == "nrel_o_m" else has_clause)
            continue

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
        elif op == "nsw":
            filters.append(~col_attr.ilike(f"{escape_like(str(value))}%"))
        elif op == "new":
            filters.append(~col_attr.ilike(f"%{escape_like(str(value))}"))
        elif op == "in":
            # RISON passes the value as a list ``!(a,b,c)``; tolerate a scalar too.
            in_vals = value if isinstance(value, list) else [value]
            filters.append(col_attr.in_(in_vals))
        elif op == "not_in":
            nin_vals = value if isinstance(value, list) else [value]
            filters.append(~col_attr.in_(nin_vals))
        elif op == "gt":
            filters.append(col_attr > value)
        elif op == "lt":
            filters.append(col_attr < value)
        elif op == "gte":
            filters.append(col_attr >= value)
        elif op == "lte":
            filters.append(col_attr <= value)

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
        # The cookie NAME is the token value itself — the frontend download
        # tracker checks for a cookie named after the token it passed, not a
        # fixed "token" key.
        headers["Set-Cookie"] = f"{token}=done; Max-Age=600; Path=/; SameSite=Lax"
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
    lists-of-dicts matching the upstream API format.
    """
    result: dict[str, Any] = {}
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

    Matches the upstream ``_prettify_column`` logic:
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
    upstream REST API format the frontend expects.

    Automatically post-processes each item:
    - ``uuid`` fields are converted to strings
    - ``changed_on``/``created_on`` datetimes are left as-is for further
      processing or JSON serialization
    """
    result = [_serialize_item(item, columns) for item in (items or [])]
    ids: list[str] = []
    for row, item in zip(result, items or [], strict=False):
        if "uuid" in row and row["uuid"] is not None:
            row["uuid"] = str(row["uuid"])
        # ``ids`` is an array of strings in the Superset OpenAPI spec;
        # cast pks to str so contract validators don't reject integer entries.
        # Populated independently of list_columns — ids is always emitted
        # even when "id" is absent from the columns list.
        row_id = row.get("id") if "id" in row else getattr(item, "id", None)
        if row_id is not None:
            ids.append(str(row_id))

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
    *,
    security_manager: Any | None = None,
    current_user: Any | None = None,
    class_permission_name: str | None = None,
    rison_params: dict[str, Any] | None = None,
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

    When ``security_manager``, ``current_user`` and ``class_permission_name``
    are provided, the permissions list is dynamically filtered against the
    current user's RBAC grants (``merge_current_user_permissions``).

    When ``rison_params`` contains a ``keys`` list, only those response keys
    are included (``set_response_key_mappings`` semantics).
    """
    if security_manager is not None and current_user is not None:
        perm_name = class_permission_name or model_name
        filtered: list[str] = []
        for perm in permissions:
            if await security_manager.can_access(perm, perm_name, user=current_user):
                filtered.append(perm)
        permissions = filtered

    from superset.info_builder.builder import build_info_payload

    payload = build_info_payload(model_name, permissions=permissions)
    if payload is None:
        payload = _build_fallback_info_payload(dao, model_name, permissions)

    # When ``keys`` is an empty list or absent, all keys are returned.
    # Valid key names mirror the get_info_schema enum:
    #   add_columns, edit_columns, filters, permissions, add_title, edit_title
    # camelCase variants (addColumns etc.) are also accepted via the
    # dual-key lookup pattern used throughout liteset.
    keys: list[str] | None = None
    if rison_params:
        keys = rison_params.get("keys")
    if keys:
        payload = {k: v for k, v in payload.items() if k in keys}

    return payload


def _build_fallback_info_payload(
    dao: Any,
    model_name: str,
    permissions: list[str],
) -> dict[str, Any]:
    """SA-introspection fallback for resources without a RESOURCE_SPECS entry.

    Produces a shape closer to the original upstream ``_info`` response than the
    previous ad-hoc implementation:

    * Column ``type`` uses Marshmallow class names (``String``, ``Integer``,
      etc.) via :func:`superset.info_builder.type_map.sa_to_marshmallow_type`
      — matching ``field.__class__.__name__`` from upstream ``_get_field_info``
      (api/__init__.py:2046).
    * No ``nullable`` key (not present in original).
    * No ``label_columns`` key (not present in original ``_info`` response).
    * ``add_title`` / ``edit_title`` auto-generated as ``"Add <Model>"`` /
      ``"Edit <Model>"``.
    * Filter operators derived via
      :func:`superset.info_builder.operators.operators_for_column`
      for every column — matching the upstream ``SQLAFilterConverter`` lookup.
    """
    import re

    from sqlalchemy import inspect as sa_inspect

    from superset.info_builder.operators import operators_for_column
    from superset.info_builder.type_map import sa_to_marshmallow_type

    def _prettify_name(name: str) -> str:
        """Upstream _prettify_name: 'HelloWorld' → 'Hello World'."""
        return re.sub(r"(?<=.)([A-Z])", r" \1", name)

    add_title = "Add " + _prettify_name(model_name)
    edit_title = "Edit " + _prettify_name(model_name)

    model_cls = getattr(dao, "model_cls", None)
    columns: list[dict[str, Any]] = []
    filters: dict[str, list[dict[str, str]]] = {}

    if model_cls is not None:
        try:
            mapper = sa_inspect(model_cls)
            for col in mapper.columns:
                col_type = col.type if hasattr(col, "type") else None
                ma_type = (
                    sa_to_marshmallow_type(col_type)
                    if col_type is not None
                    else "String"
                )
                label = re.sub(r"[._]", " ", col.key).title()
                columns.append(
                    {
                        "name": col.key,
                        "label": label,
                        "description": "",
                        "type": ma_type,
                        "required": not getattr(col, "nullable", True),
                        "unique": getattr(col, "unique", False),
                    }
                )
                try:
                    ops = operators_for_column(model_cls, col.key)
                except Exception:  # noqa: BLE001
                    ops = []
                if ops:
                    filters[col.key] = ops
        except Exception:  # noqa: BLE001
            logger.debug("Could not introspect model %s columns", model_cls)

    return {
        "add_columns": columns,
        "add_title": add_title,
        "edit_columns": columns,
        "edit_title": edit_title,
        "filters": filters,
        "permissions": permissions,
    }


_EXTRA_FIELDS_REL: dict[str, list[str]] = {
    # Only ``owners`` carries extra fields
    # (``BaseSupersetModelRestApi.extra_fields_rel_fields``).
    # ``created_by``/``changed_by`` keep ``extra: {}`` for contract parity.
    "owners": ["email", "active"],
}


async def get_related_payload(  # noqa: C901
    dao: Any,
    column_name: str,
    rison_params: dict[str, Any] | None = None,
    *,
    allowed_fields: frozenset[str] | None = None,
    base_filters: list[Any] | None = None,
    query_hook: Any | None = None,
    page: int = 0,
    page_size: int = 25,
    filter_value: str = "",
    order_rel_fields: dict[str, tuple[str, str]] | None = None,
    text_field_rel_fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build /related/{column_name} response for select dropdowns.

    Returns distinct values for a relationship column (e.g., owners, databases).
    Used by the frontend to populate select inputs in forms.

    Args:
        allowed_fields: If provided, only these column names are permitted.
            Disallowed names raise NotFoundException (HTTP 404), matching
            ``allowed_rel_fields`` behavior.
        query_hook: Optional ``Callable[[Select], Select]`` matching the
            ``EXTRA_RELATED_QUERY_FILTERS`` contract.  The hook receives the
            initial SELECT statement and must return the (possibly modified)
            SELECT statement.  SA 2.0 ``Select`` exposes ``.filter()``
            (aliased to ``.where()``), so existing hooks that call
            ``query.filter(...)`` work without modification.
        order_rel_fields: Per-column ordering config, e.g.
            ``{"owners": ("first_name", "asc"), "slices": ("slice_name", "asc")}``.
            Matches ``BaseSupersetModelRestApi.order_rel_fields``.
        text_field_rel_fields: Per-column model attribute used as the ``text``
            value instead of ``str(model)``, e.g. ``{"dashboard":
            "dashboard_title", "chart": "slice_name"}``. Matches
            ``BaseSupersetModelRestApi.text_field_rel_fields`` /
            ``_get_text_for_model`` (only the reports API sets it).
    """
    from litestar.exceptions import NotFoundException

    if allowed_fields is not None and column_name not in allowed_fields:
        raise NotFoundException()

    if rison_params is not None:
        page, page_size = extract_pagination(rison_params)
        filter_value = rison_params.get("filter", "") or filter_value

    include_ids = (rison_params or {}).get("include_ids", [])

    # Pagination with forced ids is not supported — fail early.
    if page and include_ids:
        raise SupersetValidationException(
            "Pagination with include_ids is not supported"
        )

    model_cls = getattr(dao, "model_cls", None)
    if model_cls is None:
        return {"count": 0, "result": []}

    try:
        from sqlalchemy import func, inspect as sa_inspect, select as sa_select

        mapper = sa_inspect(model_cls)
        if column_name not in mapper.relationships:
            # A name that passes the allowed_fields gate but has no SA
            # relationship must still return 404, not an empty-success payload.
            raise NotFoundException()

        rel = mapper.relationships[column_name]
        rel_model = rel.mapper.class_
        stmt = sa_select(rel_model)

        # Apply EXTRA_RELATED_QUERY_FILTERS["user"] hook.
        # The hook receives the SELECT statement and returns a (possibly
        # modified) SELECT statement.  SA 2.0 Select.filter() is aliased to
        # .where(), so callbacks that call ``query.filter(...)`` work as-is.
        if query_hook is not None and callable(query_hook):
            stmt = query_hook(stmt)

        if base_filters:
            for bf in base_filters:
                stmt = stmt.where(bf)

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
                    "dashboard_title",
                    "slice_name",
                ):
                    if hasattr(rel_model, name_col):
                        stmt = stmt.where(
                            getattr(rel_model, name_col).ilike(like_value)
                        )
                        break

        if order_rel_fields and column_name in order_rel_fields:
            order_column, order_direction = order_rel_fields[column_name]
        else:
            order_column, order_direction = "", ""
        if order_column and hasattr(rel_model, order_column):
            col_attr = getattr(rel_model, order_column)
            from sqlalchemy import asc, desc

            stmt = stmt.order_by(
                asc(col_attr) if order_direction.lower() == "asc" else desc(col_attr)
            )

        total = await dao.session.scalar(
            sa_select(func.count()).select_from(stmt.subquery())
        )
        stmt = stmt.offset(page * page_size).limit(page_size)
        result = await dao.session.execute(stmt)
        items = list(result.scalars().all())

        if include_ids:
            # By this point page==0 is guaranteed (early ValidationException above).
            # Inject extra ids that are not already in the result (include_ids forcing).
            include_stmt = sa_select(rel_model).where(rel_model.id.in_(include_ids))
            include_result = await dao.session.execute(include_stmt)
            include_items = include_result.scalars().all()
            existing_ids = {item.id for item in items}
            for item in include_items:
                if item.id not in existing_ids:
                    items.append(item)
            total = len(items)

        extra_fields = _EXTRA_FIELDS_REL.get(column_name, [])
        # A per-column override attribute wins over str(model).
        text_attr = (text_field_rel_fields or {}).get(column_name)

        def _build_item(item: Any) -> dict[str, Any]:
            # Always emit an ``extra`` key (empty dict when the related
            # column has no extra fields) — original Superset's
            # RelatedResultResponseSchema declares ``extra`` as a
            # required object, and contract tests rely on its presence.
            entry: dict[str, Any] = {
                "value": item.id,
                "text": getattr(item, text_attr) if text_attr else str(item),
                "extra": {field: getattr(item, field, None) for field in extra_fields}
                if extra_fields
                else {},
            }
            return entry

        return {
            "count": total or 0,
            "result": [_build_item(item) for item in items],
        }
    except NotFoundException:
        # Litestar's NotFoundException inherits from ValueError, so it would
        # otherwise be swallowed by the broad except below.  Re-raise so the
        # 404 propagates to the framework and reaches the client.
        raise
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
            Disallowed names raise ``NotFoundException`` (HTTP 404), matching
            ``allowed_distinct_fields`` behaviour — any column not in the set
            returns 404. Default is an *empty* set, i.e. distinct is opt-in
            per resource.
    """
    if allowed_fields is not None and column_name not in allowed_fields:
        from litestar.exceptions import NotFoundException

        raise NotFoundException()

    if rison_params is not None:
        page, page_size = extract_pagination(rison_params)
        filter_value = rison_params.get("filter", "") or filter_value

    model_cls = getattr(dao, "model_cls", None)
    if model_cls is None or not hasattr(model_cls, column_name):
        from litestar.exceptions import NotFoundException

        raise NotFoundException()

    # Check that the attribute is a real column (not a relationship).
    # ``getattr(Model, "<relationship>")`` returns an InstrumentedAttribute
    # whose ``is_not`` / ``ilike`` / ``distinct`` raise NotImplementedError —
    # upstream sidesteps this via the ``allowed_distinct_fields`` gate, but
    # callers can still reach here with stray names; treat as not-found.
    # This guard must live OUTSIDE the try block so that NotFoundException is
    # not swallowed by the broad ``except (SQLAlchemyError, AttributeError,
    # ValueError)`` below — Litestar's NotFoundException inherits from
    # ValueError (NotFoundException → ClientException → HTTPException →
    # LitestarException → ValueError).
    col = getattr(model_cls, column_name)
    col_prop = getattr(col, "property", None)
    if col_prop is None or getattr(col_prop, "columns", None) is None:
        from litestar.exceptions import NotFoundException

        raise NotFoundException()

    try:
        from sqlalchemy import func, select as sa_select

        # Match original ``DistinctFilter`` behaviour — drop NULL values
        # so ``{"text": "None", "value": null}`` doesn't pollute the
        # filter dropdown (and contract snapshots).
        base_stmt = sa_select(func.distinct(col)).where(col.is_not(None))

        if base_filters:
            for bf in base_filters:
                base_stmt = base_stmt.where(bf)

        if filter_value:
            base_stmt = base_stmt.where(col.ilike(f"{_escape_like(filter_value)}%"))

        total = await dao.session.scalar(
            sa_select(func.count()).select_from(base_stmt.subquery())
        )
        from sqlalchemy import asc as sa_asc

        stmt = base_stmt.order_by(sa_asc(col)).offset(page * page_size).limit(page_size)
        result = await dao.session.execute(stmt)
        values = [v for v in result.scalars().all() if v is not None]

        return {
            "count": total or 0,
            # Raw column value for BOTH text and value; do not stringify
            # ``text`` or non-string columns (int/datetime) will diverge.
            "result": [{"text": v, "value": v} for v in values],
        }
    except (SQLAlchemyError, AttributeError, ValueError):
        logger.warning("Failed to get distinct '%s'", column_name, exc_info=True)
        return {"count": 0, "result": []}
