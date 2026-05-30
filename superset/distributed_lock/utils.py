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
"""Utilities for the KV-backed distributed lock.

Ported 1:1 from ``superset_old/distributed_lock/utils.py``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast, TypeVar, Union

from superset.utils import json


def serialize(params: dict[str, Any]) -> str:
    """Serialize parameters into a string.

    NOTE: 1:1 with ``superset_old/distributed_lock/utils.py`` — the inner
    ``sort`` helper is defined but deliberately left unused (a long-standing
    upstream quirk), so the output is ``json.dumps(params)`` and therefore
    insertion-order dependent. Kept verbatim to preserve cross-version key
    compatibility; do NOT apply ``sort`` here without changing upstream too.
    """

    T = TypeVar(
        "T",
        bound=Union[dict[str, Any], list[Any], int, float, str, bool, None],
    )

    def sort(obj: T) -> T:
        if isinstance(obj, dict):
            return cast(T, {k: sort(v) for k, v in sorted(obj.items())})
        if isinstance(obj, list):
            return cast(T, [sort(x) for x in obj])
        return obj

    return json.dumps(params)


def get_key(namespace: str, **kwargs: Any) -> uuid.UUID:
    """Compute the deterministic UUID5 key for ``(namespace, kwargs)``."""
    return uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_DNS, namespace), serialize(kwargs))
