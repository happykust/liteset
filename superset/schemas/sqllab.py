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


class EstimateQueryCostSchema(msgspec.Struct):
    """POST /api/v1/sqllab/estimate/"""

    database_id: int
    sql: str
    schema: str | None = None
    catalog: str | None = None
    template_params: dict[str, Any] = {}


class FormatSQLSchema(msgspec.Struct):
    """POST /api/v1/sqllab/format_sql/"""

    sql: str
    engine: str | None = None


class ExecutePayloadSchema(msgspec.Struct):
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


class SqlLabPermalinkSchema(msgspec.Struct):
    """POST /api/v1/sqllab/permalink

    Accepts either a raw ``state`` dict (original contract) or typed
    top-level fields (``dbId``, ``sql``, ``schema``, etc.) which are
    merged into ``state`` during normalization.
    """

    # Original contract: opaque state dict
    state: dict[str, Any] = {}

    # Typed aliases for common fields (frontend may send these directly)
    dbId: int | None = None
    sql: str | None = None
    schema: str | None = None
    catalog: str | None = None
    autorun: bool | None = None
    templateParams: str | None = None
    queryLimit: int | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        """Merge typed fields into state when state is empty."""
        typed_fields = {
            "dbId": self.dbId,
            "sql": self.sql,
            "schema": self.schema,
            "catalog": self.catalog,
            "autorun": self.autorun,
            "templateParams": self.templateParams,
            "queryLimit": self.queryLimit,
            "name": self.name,
        }
        # Only merge non-None typed fields that are not already in state
        for key, value in typed_fields.items():
            if value is not None and key not in self.state:
                self.state[key] = value
