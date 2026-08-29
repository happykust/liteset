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
from __future__ import annotations

from typing import Any

import msgspec

#: Returned by :func:`mask_uri_password` when the URI can't be parsed at all,
#: instead of echoing the (possibly credential-bearing) raw input back.
_UNPARSEABLE_URI_PLACEHOLDER = "<invalid sqlalchemy uri>"  # noqa: S105


def filter_unset(data: dict[str, Any]) -> dict[str, Any]:
    """Filter out msgspec.UNSET values from a dict."""
    return {k: v for k, v in data.items() if v is not msgspec.UNSET}


def filter_none(data: dict[str, Any]) -> dict[str, Any]:
    """Filter out None values from a dict (for POST/create bodies)."""
    return {k: v for k, v in data.items() if v is not None}


def escape_like(value: str) -> str:
    """Escape LIKE special characters (\\, %, _) to prevent wildcard injection."""
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def mask_uri_password(uri: str) -> str:
    """Replace password component in a SQLAlchemy URI with a placeholder.

    Parses via ``make_url_safe`` (SQLAlchemy's URL parser) rather than
    ``urlparse`` — ``urlparse`` mis-splits a password containing an
    unencoded ``@`` or ``/``, and on any parse failure the previous
    implementation's bare ``except: pass`` returned the UNMODIFIED input,
    leaking the real password. A parse failure now returns a constant
    placeholder instead.
    """
    if not uri:
        return uri
    try:
        from superset.commands.database.utils import PASSWORD_MASK
        from superset.databases.utils import make_url_safe

        url = make_url_safe(uri)
    except Exception:  # noqa: BLE001
        return _UNPARSEABLE_URI_PLACEHOLDER

    if not url.password:
        return uri
    return url.set(password=PASSWORD_MASK).render_as_string(hide_password=False)
