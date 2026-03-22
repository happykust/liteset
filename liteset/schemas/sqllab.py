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
"""msgspec Structs for the SqlLab API."""

# ruff: noqa: N815  — camelCase field names required for JSON API contract parity
from __future__ import annotations

from typing import Any

import msgspec


class EstimateQueryCostBody(msgspec.Struct):
    """POST /api/v1/sqllab/estimate/"""

    database_id: int
    sql: str
    schema: str | None = None
    catalog: str | None = None
    template_params: dict[str, Any] = {}


class FormatSQLBody(msgspec.Struct):
    """POST /api/v1/sqllab/format_sql/"""

    sql: str
    engine: str | None = None


class ExecutePayloadBody(msgspec.Struct):
    """POST /api/v1/sqllab/execute/"""

    database_id: int
    sql: str
    schema: str | None = None
    catalog: str | None = None
    tab: str | None = None
    tmp_table_name: str | None = None
    select_as_cta: bool = False
    ctas_method: str = "TABLE"
    templateParams: str | None = None
    queryLimit: int | None = None
    runAsync: bool = False
    expand_data: bool = True
    client_id: str | None = None
    sql_editor_id: str | None = None


class QueryResult(msgspec.Struct):
    """Query result in response."""

    status: str = "success"
    data: list[dict[str, Any]] = []
    columns: list[dict[str, Any]] = []
    selected_columns: list[dict[str, Any]] = []
    expanded_columns: list[dict[str, Any]] = []
    query: dict[str, Any] = {}
    query_id: int | None = None


class QueryExecutionResponse(msgspec.Struct):
    """Response for POST /api/v1/sqllab/execute/"""

    status: str = "success"
    data: list[dict[str, Any]] = []
    columns: list[dict[str, Any]] = []
    query: dict[str, Any] = {}
    query_id: int | None = None


class SQLLabBootstrap(msgspec.Struct):
    """Response for GET /api/v1/sqllab/"""

    tab_state_ids: list[dict[str, Any]] = []
    databases: dict[str, Any] = {}
    queries: dict[str, Any] = {}
    user: dict[str, Any] = {}


class SqlLabPermalinkBody(msgspec.Struct):
    """POST /api/v1/sqllab/permalink"""

    state: dict[str, Any] = {}
