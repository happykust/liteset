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
"""Key-value store type definitions -- ported 1:1 from
superset_old/key_value/types.py.

Codecs are NOT duplicated here; the canonical codec hierarchy lives in
``superset.key_value.manager`` (KeyValueCodec, JsonCodec, PickleCodec).
The Marshmallow-based ``MarshmallowKeyValueCodec`` is intentionally omitted
because the Litestar migration uses msgspec for serialization.
"""

from __future__ import annotations

import enum
from typing import TypedDict, Union
from uuid import UUID

Key = Union[int, UUID]


class KeyValueFilter(TypedDict, total=False):
    """Filter dict accepted by DAO lookup methods."""

    resource: str
    id: int | None
    uuid: UUID | None


class KeyValueResource(enum.StrEnum):
    """Namespaces for key-value entries.

    Each resource maps to a distinct logical store within the ``key_value``
    table.  Ported 1:1 from ``superset_old/key_value/types.py``.
    """

    APP = "app"
    DASHBOARD_PERMALINK = "dashboard_permalink"
    EXPLORE_PERMALINK = "explore_permalink"
    METASTORE_CACHE = "superset_metastore_cache"
    LOCK = "lock"
    SQLLAB_PERMALINK = "sqllab_permalink"


class SharedKey(enum.StrEnum):
    """Well-known key names shared across subsystems.

    Used for storing salts used by permalink hashing.
    Ported 1:1 from ``superset_old/key_value/types.py``.
    """

    DASHBOARD_PERMALINK_SALT = "dashboard_permalink_salt"
    EXPLORE_PERMALINK_SALT = "explore_permalink_salt"
    SQLLAB_PERMALINK_SALT = "sqllab_permalink_salt"
