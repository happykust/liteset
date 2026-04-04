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
    """POST /api/v1/sqllab/execute/

    Field names match the frontend's mixed snake_case / camelCase
    payload exactly (same as original Marshmallow schema).
    """

    database_id: int
    sql: str
    schema: str | None = None
    catalog: str | None = None
    tab: str | None = None
    tmp_table_name: str | None = None
    select_as_cta: bool = False
    ctas_method: str = "TABLE"
    template_params: str | None = None
    # camelCase — frontend sends "queryLimit", not "query_limit"
    queryLimit: int | None = None  # noqa: N815
    # camelCase — frontend sends "runAsync", not "run_async"
    runAsync: bool = False  # noqa: N815
    expand_data: bool = True
    client_id: str | None = None
    sql_editor_id: str | None = None
    # frontend sends "json": true — ignored but accepted
    json: bool = True


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


class SqlLabPermalinkSchema(msgspec.Struct, rename="camel"):
    """POST /api/v1/sqllab/permalink

    Accepts either a raw ``state`` dict (original contract) or typed
    top-level fields (``db_id``, ``sql``, ``schema``, etc.) which are
    merged into ``state`` during normalization.
    """

    # Original contract: opaque state dict
    state: dict[str, Any] = {}

    # Typed aliases for common fields (frontend may send these directly)
    db_id: int | None = None
    sql: str | None = None
    schema: str | None = None
    catalog: str | None = None
    autorun: bool | None = None
    template_params: str | None = None
    query_limit: int | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        """Merge typed fields into state when state is empty."""
        typed_fields = {
            "dbId": self.db_id,
            "sql": self.sql,
            "schema": self.schema,
            "catalog": self.catalog,
            "autorun": self.autorun,
            "templateParams": self.template_params,
            "queryLimit": self.query_limit,
            "name": self.name,
        }
        # Only merge non-None typed fields that are not already in state
        for key, value in typed_fields.items():
            if value is not None and key not in self.state:
                self.state[key] = value
