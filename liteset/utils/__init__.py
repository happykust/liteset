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
from urllib.parse import urlparse, urlunparse

import msgspec


def filter_unset(data: dict[str, Any]) -> dict[str, Any]:
    """Filter out msgspec.UNSET values from a dict."""
    return {k: v for k, v in data.items() if v is not msgspec.UNSET}


def filter_none(data: dict[str, Any]) -> dict[str, Any]:
    """Filter out None values from a dict (for POST/create bodies)."""
    return {k: v for k, v in data.items() if v is not None}


def mask_uri_password(uri: str) -> str:
    """Replace password component in a SQLAlchemy URI with a placeholder."""
    if not uri:
        return uri
    try:
        parsed = urlparse(uri)
        if parsed.password:
            masked = parsed._replace(
                netloc=f"{parsed.username or ''}:XXXXXXXXXX@{parsed.hostname}"
                + (f":{parsed.port}" if parsed.port else ""),
            )
            return urlunparse(masked)
    except Exception:  # noqa: BLE001
        pass
    return uri
