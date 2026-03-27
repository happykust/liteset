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
"""msgspec Structs for the Row Level Security API."""

from __future__ import annotations

from typing import Annotated

import msgspec
from msgspec import Meta


class RLSPostSchema(msgspec.Struct):
    """Body for POST /api/v1/rowlevelsecurity/."""

    name: Annotated[str, Meta(min_length=1)]
    filter_type: str  # "Regular" | "Base"
    clause: Annotated[str, Meta(min_length=1)]
    description: str = ""
    tables: list[int] = []  # dataset IDs
    roles: list[int] = []  # role IDs
    group_key: str = ""


class RLSPutSchema(msgspec.Struct):
    """Body for PUT /api/v1/rowlevelsecurity/{pk}."""

    name: str | None | msgspec.UnsetType = msgspec.UNSET
    filter_type: str | None | msgspec.UnsetType = msgspec.UNSET
    clause: str | None | msgspec.UnsetType = msgspec.UNSET
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    tables: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    roles: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    group_key: str | None | msgspec.UnsetType = msgspec.UNSET
