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
"""Engine-spec registry for sync/Flask-compatible engine specs.

Provides:
  - ``BaseEngineSpec``  -- base class for all engine specs
  - ``get_engine_spec(backend, driver)`` -- factory to look up an engine spec
  - Individual engine specs for all supported database engines.
"""

from __future__ import annotations

from superset.db_engine_specs.ascend import AscendEngineSpec
from superset.db_engine_specs.athena import AthenaEngineSpec
from superset.db_engine_specs.aurora import AuroraMySQLDataAPI, AuroraPostgresDataAPI
from superset.db_engine_specs.base import BaseEngineSpec
from superset.db_engine_specs.bigquery import BigQueryEngineSpec
from superset.db_engine_specs.clickhouse import (
    ClickHouseBaseEngineSpec,
    ClickHouseConnectEngineSpec,
    ClickHouseEngineSpec,
)
from superset.db_engine_specs.cockroachdb import CockroachDbEngineSpec
from superset.db_engine_specs.couchbase import CouchbaseEngineSpec
from superset.db_engine_specs.crate import CrateEngineSpec
from superset.db_engine_specs.databend import (
    DatabendBaseEngineSpec,
    DatabendConnectEngineSpec,
    DatabendEngineSpec,
)
from superset.db_engine_specs.databricks import DatabricksNativeEngineSpec
from superset.db_engine_specs.db2 import Db2EngineSpec
from superset.db_engine_specs.denodo import DenodoEngineSpec
from superset.db_engine_specs.doris import DorisEngineSpec
from superset.db_engine_specs.dremio import DremioEngineSpec
from superset.db_engine_specs.drill import DrillEngineSpec
from superset.db_engine_specs.druid import DruidEngineSpec
from superset.db_engine_specs.duckdb import DuckDBEngineSpec
from superset.db_engine_specs.dynamodb import DynamoDBEngineSpec
from superset.db_engine_specs.elasticsearch import (
    ElasticSearchEngineSpec,
    OpenDistroEngineSpec,
)
from superset.db_engine_specs.exasol import ExasolEngineSpec
from superset.db_engine_specs.firebird import FirebirdEngineSpec
from superset.db_engine_specs.firebolt import FireboltEngineSpec
from superset.db_engine_specs.gsheets import GSheetsEngineSpec
from superset.db_engine_specs.hana import HanaEngineSpec
from superset.db_engine_specs.hive import HiveEngineSpec
from superset.db_engine_specs.ibmi import IBMiEngineSpec
from superset.db_engine_specs.impala import ImpalaEngineSpec
from superset.db_engine_specs.kusto import KustoKqlEngineSpec, KustoSqlEngineSpec
from superset.db_engine_specs.kylin import KylinEngineSpec
from superset.db_engine_specs.mariadb import MariaDBEngineSpec
from superset.db_engine_specs.mssql import MssqlEngineSpec
from superset.db_engine_specs.mysql import MySQLEngineSpec
from superset.db_engine_specs.netezza import NetezzaEngineSpec
from superset.db_engine_specs.oceanbase import OceanBaseEngineSpec
from superset.db_engine_specs.ocient import OcientEngineSpec
from superset.db_engine_specs.oracle import OracleEngineSpec
from superset.db_engine_specs.parseable import ParseableEngineSpec
from superset.db_engine_specs.pinot import PinotEngineSpec
from superset.db_engine_specs.postgres import (
    PostgresBaseEngineSpec,
    PostgresEngineSpec,
)
from superset.db_engine_specs.presto import PrestoEngineSpec
from superset.db_engine_specs.redshift import RedshiftEngineSpec
from superset.db_engine_specs.risingwave import RisingWaveDbEngineSpec
from superset.db_engine_specs.shillelagh import ShillelaghEngineSpec
from superset.db_engine_specs.singlestore import SingleStoreSpec
from superset.db_engine_specs.snowflake import SnowflakeEngineSpec
from superset.db_engine_specs.solr import SolrEngineSpec
from superset.db_engine_specs.spark import SparkEngineSpec
from superset.db_engine_specs.sqlite import SqliteEngineSpec
from superset.db_engine_specs.starrocks import StarRocksEngineSpec
from superset.db_engine_specs.superset import SupersetEngineSpec
from superset.db_engine_specs.tdengine import TDengineEngineSpec
from superset.db_engine_specs.teradata import TeradataEngineSpec
from superset.db_engine_specs.trino import PrestoBaseEngineSpec, TrinoEngineSpec
from superset.db_engine_specs.vertica import VerticaEngineSpec
from superset.db_engine_specs.ydb import YDBEngineSpec


# ---------------------------------------------------------------------------
# Registry of known engine specs keyed by engine name + aliases.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[BaseEngineSpec]] = {}


def _register(spec: type[BaseEngineSpec]) -> None:
    """Register a spec under its ``engine`` and all ``engine_aliases``."""
    if engine := getattr(spec, "engine", ""):
        _REGISTRY[engine] = spec
    for alias in getattr(spec, "engine_aliases", set()):
        _REGISTRY[alias] = spec


# Register known specs — original 22 engines.
_register(PostgresEngineSpec)
_register(MySQLEngineSpec)
_register(SqliteEngineSpec)
_register(ClickHouseEngineSpec)
_register(ClickHouseConnectEngineSpec)
_register(TrinoEngineSpec)
_register(BigQueryEngineSpec)
_register(MssqlEngineSpec)
_register(OracleEngineSpec)
_register(SnowflakeEngineSpec)
_register(AthenaEngineSpec)
_register(RedshiftEngineSpec)
_register(DatabricksNativeEngineSpec)
_register(DuckDBEngineSpec)
_register(DruidEngineSpec)
_register(HiveEngineSpec)
_register(PrestoEngineSpec)
_register(StarRocksEngineSpec)
_register(DorisEngineSpec)
_register(VerticaEngineSpec)
_register(ImpalaEngineSpec)
_register(PinotEngineSpec)
_register(CockroachDbEngineSpec)

# Register new 35 engines.
_register(AscendEngineSpec)
_register(AuroraMySQLDataAPI)
_register(AuroraPostgresDataAPI)
_register(CouchbaseEngineSpec)
_register(CrateEngineSpec)
_register(DatabendEngineSpec)
_register(DatabendConnectEngineSpec)
_register(Db2EngineSpec)
_register(DenodoEngineSpec)
_register(DremioEngineSpec)
_register(DrillEngineSpec)
_register(DynamoDBEngineSpec)
_register(ElasticSearchEngineSpec)
_register(OpenDistroEngineSpec)
_register(ExasolEngineSpec)
_register(FirebirdEngineSpec)
_register(FireboltEngineSpec)
_register(GSheetsEngineSpec)
_register(HanaEngineSpec)
_register(IBMiEngineSpec)
_register(KustoSqlEngineSpec)
_register(KustoKqlEngineSpec)
_register(KylinEngineSpec)
_register(MariaDBEngineSpec)
_register(NetezzaEngineSpec)
_register(OceanBaseEngineSpec)
_register(OcientEngineSpec)
_register(ParseableEngineSpec)
_register(RisingWaveDbEngineSpec)
_register(ShillelaghEngineSpec)
_register(SingleStoreSpec)
_register(SolrEngineSpec)
_register(SparkEngineSpec)
_register(SupersetEngineSpec)
_register(TDengineEngineSpec)
_register(TeradataEngineSpec)
_register(YDBEngineSpec)


def get_engine_spec(
    backend: str,
    driver: str | None = None,
) -> type[BaseEngineSpec]:
    """Return the engine spec for *backend* (and optionally *driver*).

    Falls back to ``BaseEngineSpec`` when no specific spec is registered.
    """
    if spec := _REGISTRY.get(backend):
        return spec
    return BaseEngineSpec


__all__ = [
    "AscendEngineSpec",
    "AthenaEngineSpec",
    "AuroraMySQLDataAPI",
    "AuroraPostgresDataAPI",
    "BaseEngineSpec",
    "BigQueryEngineSpec",
    "ClickHouseBaseEngineSpec",
    "ClickHouseConnectEngineSpec",
    "ClickHouseEngineSpec",
    "CockroachDbEngineSpec",
    "CouchbaseEngineSpec",
    "CrateEngineSpec",
    "DatabendBaseEngineSpec",
    "DatabendConnectEngineSpec",
    "DatabendEngineSpec",
    "DatabricksNativeEngineSpec",
    "Db2EngineSpec",
    "DenodoEngineSpec",
    "DorisEngineSpec",
    "DremioEngineSpec",
    "DrillEngineSpec",
    "DruidEngineSpec",
    "DuckDBEngineSpec",
    "DynamoDBEngineSpec",
    "ElasticSearchEngineSpec",
    "ExasolEngineSpec",
    "FirebirdEngineSpec",
    "FireboltEngineSpec",
    "GSheetsEngineSpec",
    "HanaEngineSpec",
    "HiveEngineSpec",
    "IBMiEngineSpec",
    "ImpalaEngineSpec",
    "KustoKqlEngineSpec",
    "KustoSqlEngineSpec",
    "KylinEngineSpec",
    "MariaDBEngineSpec",
    "MssqlEngineSpec",
    "MySQLEngineSpec",
    "NetezzaEngineSpec",
    "OceanBaseEngineSpec",
    "OcientEngineSpec",
    "OpenDistroEngineSpec",
    "OracleEngineSpec",
    "ParseableEngineSpec",
    "PinotEngineSpec",
    "PostgresBaseEngineSpec",
    "PostgresEngineSpec",
    "PrestoBaseEngineSpec",
    "PrestoEngineSpec",
    "RedshiftEngineSpec",
    "RisingWaveDbEngineSpec",
    "ShillelaghEngineSpec",
    "SingleStoreSpec",
    "SnowflakeEngineSpec",
    "SolrEngineSpec",
    "SparkEngineSpec",
    "SqliteEngineSpec",
    "StarRocksEngineSpec",
    "SupersetEngineSpec",
    "TDengineEngineSpec",
    "TeradataEngineSpec",
    "TrinoEngineSpec",
    "VerticaEngineSpec",
    "YDBEngineSpec",
    "get_engine_spec",
]
