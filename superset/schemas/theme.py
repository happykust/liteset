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

from typing import Annotated, Any

import msgspec
from msgspec import Meta


def _validate_theme_json(value: Any) -> None:
    """Reject unparseable JSON in ``json_data`` up-front.

    A PUT with ``json_data='{invalid'`` would silently persist broken text;
    subsequent reads would feed it to the SPA's ``JSON.parse`` and crash
    the theme picker.
    """
    if value is None or value is msgspec.UNSET:
        return
    if not isinstance(value, str) or value == "":
        return
    import json as _json

    try:
        _json.loads(value)
    except (ValueError, TypeError) as ex:
        raise msgspec.ValidationError(f"json_data is not valid JSON: {ex}") from ex


class ThemePostSchema(msgspec.Struct):
    """POST /api/v1/theme/

    Accepts ``theme_name`` and the serialized ``json_data`` only.
    The legacy ``css``/``json_metadata``/``description`` fields belonged
    to a different (CSS template) entity and are not part of the
    ``themes`` table schema.
    """

    theme_name: Annotated[str, Meta(min_length=1)]
    json_data: str

    def __post_init__(self) -> None:
        _validate_theme_json(self.json_data)


class ThemePutSchema(msgspec.Struct):
    """PUT /api/v1/theme/<pk>

    BOTH fields are ``required=True, allow_none=False`` — a partial PUT or an explicit
    null is a 400 upstream, never a silent NULL write.
    """

    theme_name: Annotated[str, Meta(min_length=1)]
    json_data: str

    def __post_init__(self) -> None:
        _validate_theme_json(self.json_data)
