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
"""CSRF configuration for Liteset.

Replaces Flask-WTF CSRF protection with Litestar's built-in CSRFConfig.
"""
from __future__ import annotations

from litestar.config.csrf import CSRFConfig


def create_csrf_config(
    secret: str,
    *,
    cookie_name: str = "csrf_access_token",
    header_name: str = "X-CSRFToken",
    safe_methods: set[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> CSRFConfig:
    """Create CSRF configuration for the Litestar app.

    Args:
        secret: Secret key for CSRF token signing.
        cookie_name: Name of the CSRF cookie.
        header_name: Name of the CSRF header.
        safe_methods: HTTP methods exempt from CSRF (default: GET, HEAD, OPTIONS).
        exclude_paths: URL paths to exclude from CSRF protection.

    Returns:
        Configured CSRFConfig instance.
    """
    if safe_methods is None:
        safe_methods = {"GET", "HEAD", "OPTIONS"}

    return CSRFConfig(
        secret=secret,
        cookie_name=cookie_name,
        header_name=header_name,
        safe_methods=safe_methods,  # type: ignore[arg-type]
        exclude=exclude_paths or [],
    )
