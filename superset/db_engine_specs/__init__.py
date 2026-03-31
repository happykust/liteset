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
"""Compatibility shim for ``superset.db_engine_specs``.

Legacy migrations import:
  - ``BaseEngineSpec``
  - ``get_engine_spec``
"""
from __future__ import annotations

from superset.db_engine_specs.base import BaseEngineSpec


def get_engine_spec(
    backend: str,
    driver: str | None = None,
) -> type[BaseEngineSpec]:
    """Return the engine spec for *backend* (and optionally *driver*).

    This shim always returns ``BaseEngineSpec`` since the migration only needs
    basic engine-spec functionality and the full plugin registry is not available.
    """
    return BaseEngineSpec


__all__ = ["BaseEngineSpec", "get_engine_spec"]
