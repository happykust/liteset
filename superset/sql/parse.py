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
"""
Compatibility shim for Alembic migrations that import Table and SQLScript.

This is a minimal reproduction of the original superset.sql.parse module,
containing only what is needed by migrations.
"""
from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialects
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

# ---------------------------------------------------------------------------
# Dialect mapping (engine name -> sqlglot dialect)
# ---------------------------------------------------------------------------
SQLGLOT_DIALECTS: dict[str, str] = {
    "base": Dialects.DIALECT,
    "ascend": Dialects.HIVE,
    "awsathena": Dialects.PRESTO,
    "bigquery": Dialects.BIGQUERY,
    "clickhouse": Dialects.CLICKHOUSE,
    "clickhousedb": Dialects.CLICKHOUSE,
    "cockroachdb": Dialects.POSTGRES,
    "couchbase": Dialects.MYSQL,
    "databricks": Dialects.DATABRICKS,
    "drill": Dialects.DRILL,
    "druid": Dialects.DRUID,
    "duckdb": Dialects.DUCKDB,
    "gsheets": Dialects.SQLITE,
    "hana": Dialects.POSTGRES,
    "hive": Dialects.HIVE,
    "impala": Dialects.HIVE,
    "mariadb": Dialects.MYSQL,
    "motherduck": Dialects.DUCKDB,
    "mssql": Dialects.TSQL,
    "mysql": Dialects.MYSQL,
    "netezza": Dialects.POSTGRES,
    "oceanbase": Dialects.MYSQL,
    "oracle": Dialects.ORACLE,
    "postgresql": Dialects.POSTGRES,
    "presto": Dialects.PRESTO,
    "pydoris": Dialects.DORIS,
    "redshift": Dialects.REDSHIFT,
    "risingwave": Dialects.POSTGRES,
    "shillelagh": Dialects.SQLITE,
    "singlestore": Dialects.MYSQL,
    "snowflake": Dialects.SNOWFLAKE,
    "spark": Dialects.SPARK,
    "sqlite": Dialects.SQLITE,
    "starrocks": Dialects.STARROCKS,
    "teradata": Dialects.TERADATA,
    "trino": Dialects.TRINO,
    "vertica": Dialects.POSTGRES,
}


# ---------------------------------------------------------------------------
# Table dataclass
# ---------------------------------------------------------------------------
@dataclass(eq=True, frozen=True)
class Table:
    """
    A fully qualified SQL table conforming to [[catalog.]schema.]table.
    """

    table: str
    schema: str | None = None
    catalog: str | None = None

    def __str__(self) -> str:
        return ".".join(
            urllib.parse.quote(part, safe="").replace(".", "%2E")
            for part in [self.catalog, self.schema, self.table]
            if part
        )

    def __eq__(self, other: Any) -> bool:
        return str(self) == str(other)

    def qualify(
        self,
        *,
        catalog: str | None = None,
        schema: str | None = None,
    ) -> Table:
        return Table(
            table=self.table,
            schema=self.schema or schema,
            catalog=self.catalog or catalog,
        )


# ---------------------------------------------------------------------------
# Helper: extract tables from a single parsed expression
# ---------------------------------------------------------------------------
def _is_cte(source: exp.Table, scope: Scope) -> bool:
    parent_sources = scope.parent.sources if scope.parent else {}
    ctes_in_scope = {
        name
        for name, parent_scope in parent_sources.items()
        if isinstance(parent_scope, Scope) and parent_scope.scope_type == ScopeType.CTE
    }
    return source.name in ctes_in_scope


def extract_tables_from_statement(
    statement: exp.Expression,
    dialect: str | None,
) -> set[Table]:
    sources: Iterable[exp.Table]

    if isinstance(statement, exp.Describe):
        sources = statement.find_all(exp.Table)
    elif isinstance(statement, exp.Command):
        literal = statement.find(exp.Literal)
        if not literal:
            return set()
        try:
            pseudo_query = sqlglot.parse_one(f"SELECT {literal.this}", dialect=dialect)
        except ParseError:
            return set()
        sources = pseudo_query.find_all(exp.Table)
    else:
        sources = [
            source
            for scope in traverse_scope(statement)
            for source in scope.sources.values()
            if isinstance(source, exp.Table) and not _is_cte(source, scope)
        ]

    return {
        Table(
            source.name,
            source.db if source.db != "" else None,
            source.catalog if source.catalog != "" else None,
        )
        for source in sources
    }


# ---------------------------------------------------------------------------
# SQLStatement — wraps a single parsed statement
# ---------------------------------------------------------------------------
class SQLStatement:
    """A single SQL statement with table extraction."""

    def __init__(
        self,
        statement: str | None = None,
        engine: str = "base",
        ast: exp.Expression | None = None,
    ):
        self.engine = engine
        self._dialect = SQLGLOT_DIALECTS.get(engine)

        if ast is not None:
            self._parsed = ast
        elif statement:
            parsed = sqlglot.parse(statement, dialect=self._dialect)
            if len(parsed) != 1 or parsed[0] is None:
                raise ValueError("Expected exactly one statement")
            self._parsed = parsed[0]
        else:
            raise ValueError("Either statement or ast must be provided")

        self.tables = extract_tables_from_statement(self._parsed, self._dialect)

    @classmethod
    def split_script(
        cls,
        script: str,
        engine: str,
    ) -> list[SQLStatement]:
        dialect = SQLGLOT_DIALECTS.get(engine)
        try:
            statements = sqlglot.parse(script, dialect=dialect)
        except Exception:
            return []

        return [
            cls(ast=ast, engine=engine)
            for ast in statements
            if ast is not None
        ]


# ---------------------------------------------------------------------------
# SQLScript — wraps a full script of 0+ statements
# ---------------------------------------------------------------------------
class SQLScript:
    """A SQL script, with 0+ statements."""

    def __init__(
        self,
        script: str,
        engine: str = "base",
    ):
        self.engine = engine
        self.statements = SQLStatement.split_script(script, engine)

    def format(self, comments: bool = True) -> str:
        return ";\n".join(
            self._format_statement(stmt) for stmt in self.statements
        )

    @staticmethod
    def _format_statement(stmt: SQLStatement) -> str:
        return stmt._parsed.sql()  # noqa: SLF001

    def has_mutation(self) -> bool:
        mutating_nodes = (
            exp.Insert, exp.Update, exp.Delete, exp.Merge,
            exp.Create, exp.Drop, exp.TruncateTable, exp.Alter,
        )
        return any(
            stmt._parsed.find(*mutating_nodes)  # noqa: SLF001
            for stmt in self.statements
        )
