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
"""Compatibility shim for ``superset.databases.utils``.

Legacy migrations import:
  - ``make_url_safe``
"""
from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.engine.url import URL


class DatabaseInvalidError(Exception):
    """Database parameters are invalid."""


def make_url_safe(raw_url: str | URL) -> URL:
    """Wrapper for SQLAlchemy ``make_url()`` that masks sensitive details in errors."""
    if isinstance(raw_url, str):
        url = raw_url.strip()
        try:
            return make_url(url)
        except Exception as ex:
            raise DatabaseInvalidError() from ex

    return raw_url


__all__ = ["make_url_safe", "DatabaseInvalidError"]
