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
# mypy: ignore-errors
"""Async port of ``superset_old/commands/database/utils.py``.

Local helper utilities shared between database commands.
"""

from __future__ import annotations

import json
import re
from typing import Any

EXPORT_VERSION = "1.0.0"

# Regex to sanitize file names (remove unsafe characters)
_SAFE_FILENAME_RE = re.compile(r"[^\w\s\-.]")

PASSWORD_MASK = "XXXXXXXXXX"  # noqa: S105


def _safe_filename(name: str) -> str:
    """Create a safe filename from a model name (like werkzeug's secure_filename)."""
    name = _SAFE_FILENAME_RE.sub("", name).strip()
    return name or "unnamed"


def _parse_extra(extra_payload: str) -> dict[str, Any]:
    """Parse the extra JSON field from a Database, with legacy fixups."""
    try:
        extra = json.loads(extra_payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    # Fix for DBs saved with an invalid ``schemas_allowed_for_csv_upload``
    schemas_allowed = extra.get("schemas_allowed_for_csv_upload")
    if isinstance(schemas_allowed, str):
        try:
            extra["schemas_allowed_for_csv_upload"] = json.loads(schemas_allowed)
        except (json.JSONDecodeError, TypeError):
            pass
    return extra


def _mask_ssh_tunnel_passwords(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask password fields in an SSH tunnel export payload."""
    masked = dict(payload)
    for key in ("password", "private_key", "private_key_password"):
        if masked.get(key):
            masked[key] = PASSWORD_MASK
    return masked
