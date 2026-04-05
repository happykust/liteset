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
# mypy: ignore-errors
"""Databricks engine spec -- sync/Flask-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/databricks.py`` with Flask
imports removed.  Only overridden methods and attributes are included.

This file provides a simplified ``DatabricksNativeEngineSpec`` for the
connector driver.  The Hive-based and ODBC-based variants are omitted
since they depend on heavy Flask/pyhive imports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import types
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import URL

from superset.constants import TimeGrain
from superset.db_engine_specs.base import BaseEngineSpec

if TYPE_CHECKING:
    from superset.models.core import Database

# ---------------------------------------------------------------------------
# Shared time grain expressions for all Databricks variants
# ---------------------------------------------------------------------------

_time_grain_expressions: dict[str | None, str] = {
    None: "{col}",
    TimeGrain.SECOND: "date_trunc('second', {col})",
    TimeGrain.MINUTE: "date_trunc('minute', {col})",
    TimeGrain.HOUR: "date_trunc('hour', {col})",
    TimeGrain.DAY: "date_trunc('day', {col})",
    TimeGrain.WEEK: "date_trunc('week', {col})",
    TimeGrain.MONTH: "date_trunc('month', {col})",
    TimeGrain.QUARTER: "date_trunc('quarter', {col})",
    TimeGrain.YEAR: "date_trunc('year', {col})",
    TimeGrain.WEEK_ENDING_SATURDAY: (
        "date_trunc('week', {col} + interval '1 day') + interval '5 days'"
    ),
    TimeGrain.WEEK_STARTING_SUNDAY: (
        "date_trunc('week', {col} + interval '1 day') - interval '1 day'"
    ),
}


class DatabricksNativeEngineSpec(BaseEngineSpec):
    """Databricks engine spec using the ``databricks+connector`` driver."""

    engine = "databricks"
    engine_name = "Databricks"
    default_driver = "connector"

    supports_dynamic_schema = True
    supports_catalog = True
    supports_dynamic_catalog = True
    supports_cross_catalog_queries = True

    _time_grain_expressions = _time_grain_expressions

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"CAST('{dttm.date().isoformat()}' AS DATE)"
        if isinstance(sqla_type, types.TIMESTAMP):
            return (
                f"""CAST('{dttm.isoformat(sep=" ", timespec="microseconds")}' """
                "AS TIMESTAMP)"
            )
        return None

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "from_unixtime({col})"

    @classmethod
    def get_catalog_names(
        cls,
        database: Database,
        inspector: Inspector,
    ) -> set[str]:
        return {catalog for (catalog,) in inspector.bind.execute("SHOW CATALOGS")}

    @classmethod
    def get_prequeries(
        cls,
        database: Database,
        catalog: str | None = None,
        schema: str | None = None,
    ) -> list[str]:
        prequeries: list[str] = []
        if catalog:
            catalog = f"`{catalog}`" if not catalog.startswith("`") else catalog
            prequeries.append(f"USE CATALOG {catalog}")
        if schema:
            schema = f"`{schema}`" if not schema.startswith("`") else schema
            prequeries.append(f"USE SCHEMA {schema}")
        return prequeries

    @classmethod
    def adjust_engine_params(
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        if catalog:
            uri = uri.update_query_dict({"catalog": catalog})
        if schema:
            uri = uri.update_query_dict({"schema": schema})
        return uri, connect_args

    @classmethod
    def get_default_catalog(cls, database: Database) -> str | None:
        return database.url_object.query.get("catalog")


__all__ = [
    "DatabricksNativeEngineSpec",
]
