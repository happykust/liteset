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
"""msgspec Structs for the Theme API."""

from __future__ import annotations

from typing import Annotated

import msgspec
from msgspec import Meta


class ThemePostSchema(msgspec.Struct):
    """POST /api/v1/theme/

    Mirrors original ``ThemePostSchema`` at
    superset_old/themes/schemas.py:83 — accepts ``theme_name`` and the
    serialized ``json_data`` only. The legacy ``css``/``json_metadata``/
    ``description`` fields belonged to a different (CSS template) entity
    and are not part of the ``themes`` table schema.
    """

    theme_name: Annotated[str, Meta(min_length=1)]
    json_data: str


class ThemePutSchema(msgspec.Struct):
    """PUT /api/v1/theme/<pk>"""

    theme_name: str | None | msgspec.UnsetType = msgspec.UNSET
    json_data: str | None | msgspec.UnsetType = msgspec.UNSET
