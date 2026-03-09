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

import msgspec


class ApiResponse(msgspec.Struct):
    result: Any = None
    id: int | str | None = None
    message: str | None = None


class ApiListResponse(msgspec.Struct):
    result: list[Any] = []
    count: int = 0
    ids: list[int | str] = []
    label_columns: dict[str, str] = {}
    list_columns: list[str] = []
    order_columns: list[str] = []
    description_columns: dict[str, str] = {}


class ErrorResponse(msgspec.Struct):
    message: str = ""
    errors: dict[str, list[str]] = {}
    status: int = 400
