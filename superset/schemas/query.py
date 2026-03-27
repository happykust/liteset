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
"""msgspec Structs for the Query and SavedQuery APIs."""

from __future__ import annotations

from typing import Any

import msgspec

from superset.schemas.base import ApiListResponse, ApiResponse


class QueryResponse(msgspec.Struct):
    """Query object in response."""

    id: int | None = None
    changed_on: str | None = None
    database: dict[str, Any] = {}
    end_result_backend_time: float | None = None
    end_time: float | None = None
    error_message: str | None = None
    executed_sql: str | None = None
    limit: int | None = None
    progress: int = 0
    rows: int | None = None
    schema: str | None = None
    sql: str | None = None
    sql_editor_id: str | None = None
    start_running_time: float | None = None
    start_time: float | None = None
    status: str | None = None
    tab_name: str | None = None
    tmp_schema_name: str | None = None
    tmp_table_name: str | None = None
    tracking_url: str | None = None


class StopQuerySchema(msgspec.Struct):
    """POST /api/v1/query/stop"""

    client_id: str


class QueryDatabaseInfo(msgspec.Struct):
    """Nested DB info in query response."""

    id: int
    database_name: str = ""


class ImportV1SavedQuery(msgspec.Struct):
    """Import payload for a saved query."""

    sql: str
    uuid: str
    version: str
    database_uuid: str
    schema: str | None = None
    label: str = ""
    description: str | None = None
    catalog: str | None = None


class SavedQueryPostSchema(msgspec.Struct):
    """POST /api/v1/saved_query/"""

    label: str
    sql: str
    db_id: int
    schema: str | None = None
    description: str | None = None
    template_params: str | None = None
    tags: list[dict[str, Any]] | None = None
    catalog: str | None = None


class SavedQueryPutSchema(msgspec.Struct):
    """PUT /api/v1/saved_query/<pk>"""

    label: str | None | msgspec.UnsetType = msgspec.UNSET
    sql: str | None | msgspec.UnsetType = msgspec.UNSET
    db_id: int | None | msgspec.UnsetType = msgspec.UNSET
    schema: str | None | msgspec.UnsetType = msgspec.UNSET
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    template_params: str | None | msgspec.UnsetType = msgspec.UNSET
    tags: list[dict[str, Any]] | None | msgspec.UnsetType = msgspec.UNSET
    catalog: str | None | msgspec.UnsetType = msgspec.UNSET


QueryGetResponse = ApiResponse
QueryListResponse = ApiListResponse
SavedQueryGetResponse = ApiResponse
SavedQueryListResponse = ApiListResponse
