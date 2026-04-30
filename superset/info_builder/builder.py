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
"""Dynamic ``GET /<resource>/_info`` payload builder.

Replaces the previous static JSON fixtures in ``superset/info_specs/``.
The builder composes the response from three sources:

* :mod:`superset.info_builder.specs` — static ``add_columns`` /
  ``edit_columns`` definitions (Marshmallow shape) and custom search
  filters.  This is where contract-specific knowledge lives.
* :mod:`superset.info_builder.operators` — SA-introspection-based
  filter operator catalogues per column type.  This part is fully
  derived from the live SQLAlchemy model.
* Caller-provided ``permissions`` — runtime RBAC for the request.

Together this gives a payload byte-equivalent to the original Apache
Superset ``_info`` endpoint without the Flask-AppBuilder dependency.
"""

from __future__ import annotations

import re
from typing import Any

from superset.info_builder.operators import operators_for_column
from superset.info_builder.specs import FieldSpec, RESOURCE_SPECS


def build_info_payload(
    model_name: str,
    permissions: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build the dynamic ``_info`` payload for ``model_name``.

    Args:
        model_name: Canonical model class name as passed to
            :func:`superset.controllers.base.get_info_payload`
            (e.g. ``"Chart"``, ``"Slice"``, ``"SqlaTable"``).
        permissions: Permissions catalogue for the current user/role
            (typically ``["can_read", "can_write"]``).  Defaults to
            ``["can_read", "can_write"]`` when ``None``.

    Returns:
        The fully-assembled payload, or ``None`` when no descriptor is
        registered for ``model_name`` — callers should then fall back to
        the legacy SA-introspection payload.
    """
    spec = RESOURCE_SPECS.get(model_name)
    if spec is None:
        return None

    model_cls = _resolve_model(model_name)

    add_columns = [_field_to_dict(f) for f in spec.add_columns]
    edit_columns = [_field_to_dict(f) for f in spec.edit_columns]

    filters: dict[str, list[dict[str, str]]] = {}
    for col in spec.search_columns:
        try:
            ops = operators_for_column(model_cls, col) if model_cls else []
        except KeyError:
            # Virtual / mixin-provided column not present in
            # ``__mapper__`` — only the explicitly-declared custom
            # filters apply (see e.g. ``dataset.sql``).
            ops = []
        custom = spec.search_filters_custom.get(col, [])
        filters[col] = list(ops) + list(custom)

    perms = list(permissions) if permissions else ["can_read", "can_write"]

    return {
        "add_columns": add_columns,
        "add_title": spec.add_title,
        "edit_columns": edit_columns,
        "edit_title": spec.edit_title,
        "filters": filters,
        "permissions": perms,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _field_to_dict(f: FieldSpec) -> dict[str, Any]:
    """Render one ``FieldSpec`` into the snapshot Marshmallow dict shape.

    ``validate`` is omitted entirely when empty — matching the snapshot
    which does not include the key for fields without validators.
    """
    out: dict[str, Any] = {
        "description": f.description,
        "label": _prettify(f.name),
        "name": f.name,
        "required": f.required,
        "type": f.type,
        "unique": f.unique,
    }
    if f.validate:
        out["validate"] = list(f.validate)
    return out


def _prettify(name: str) -> str:
    """FAB ``_prettify_column`` — ``slice_name`` → ``"Slice Name"``."""
    return re.sub(r"[._]", " ", name).title()


def _resolve_model(model_name: str) -> type | None:
    """Map ``model_name`` to its SA model class (lazy import).

    Returns ``None`` for descriptors not associated with a SA model
    (none currently — every spec ships with one).
    """
    if model_name in {"Chart", "Slice"}:
        from superset.models.slice import Slice

        return Slice
    if model_name == "Dashboard":
        from superset.models.dashboard import Dashboard

        return Dashboard
    if model_name in {"SqlaTable", "Dataset"}:
        from superset.models.connectors import SqlaTable

        return SqlaTable
    if model_name == "CssTemplate":
        from superset.models.core import CssTemplate

        return CssTemplate
    if model_name == "Theme":
        from superset.models.core import Theme

        return Theme
    if model_name in {"AnnotationLayer", "Annotation"}:
        # ``/api/v1/annotation_layer/_info`` historically exposes
        # ``Annotation`` (sub-resource) field metadata — see the
        # contract snapshot ``annotation_layer_info.json`` whose
        # add_columns are ``short_descr/long_descr/start_dttm/...``.
        from superset.models.annotations import Annotation

        return Annotation
    if model_name == "SavedQuery":
        from superset.models.sql_lab import SavedQuery

        return SavedQuery
    if model_name in {"ReportSchedule", "Report"}:
        from superset.models.reports import ReportSchedule

        return ReportSchedule
    return None


__all__ = ["build_info_payload"]
