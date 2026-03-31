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
"""Compatibility shim for ``superset.connectors.sqla.utils``.

Legacy migrations import:
  - ``get_identifier_quoter``
"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.engine import make_url as SqlaURL_create


def get_identifier_quoter(drivername: str) -> Callable[[str], str]:
    """Return an identifier quoting function for the given SQLAlchemy driver."""
    return SqlaURL_create(f"{drivername}://").get_dialect()().identifier_preparer.quote


__all__ = ["get_identifier_quoter"]
