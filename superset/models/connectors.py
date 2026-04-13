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
"""Connector models: SqlaTable, TableColumn, SqlMetric, RowLevelSecurityFilter.

Pure SQLAlchemy -- no Flask dependencies.
Includes async_query() for chart data execution via the async engine specs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    JSON,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    CertificationMixin,
    ExploreMixin,
    ImportExportMixin,
    MediumText,
    metadata,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _escape_sql_string(value: str) -> str:
    """Escape single quotes in a SQL literal value."""
    return value.replace("'", "''")


# SQL reserved keywords (conservative superset of ANSI SQL and common
# dialect reservations). Used by ``SqlaTable._quote_col_if_needed`` to
# decide whether a simple identifier needs quoting. Kept in sync with the
# list SQLAlchemy uses internally for default quoting behaviour.
_SQL_RESERVED_WORDS: frozenset[str] = frozenset(
    {
        "all", "analyse", "analyze", "and", "any", "array", "as", "asc",
        "asymmetric", "both", "case", "cast", "check", "collate", "column",
        "constraint", "create", "current_catalog", "current_date",
        "current_role", "current_time", "current_timestamp", "current_user",
        "default", "deferrable", "desc", "distinct", "do", "else", "end",
        "except", "false", "fetch", "for", "foreign", "from", "grant",
        "group", "having", "in", "initially", "intersect", "into", "lateral",
        "leading", "limit", "localtime", "localtimestamp", "not", "null",
        "offset", "on", "only", "or", "order", "placing", "primary",
        "references", "returning", "select", "session_user", "some",
        "symmetric", "table", "then", "to", "trailing", "true", "union",
        "unique", "user", "using", "variadic", "when", "where", "window",
        "with",
        # additional commonly-reserved identifiers across engines
        "date", "time", "timestamp", "year", "month", "day", "hour",
        "minute", "second", "level", "number", "position", "value",
    }
)


def _parse_dttm(value: Any) -> datetime | None:
    """Coerce a datetime-ish value to a datetime object or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        if not value or value.lower() in ("", "no filter"):
            return None
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            pass
        # Try dateutil as fallback
        try:
            import dateutil.parser  # type: ignore[import-untyped]

            return dateutil.parser.parse(value)
        except (ValueError, TypeError, ImportError):
            return None
    return None


# ---------------------------------------------------------------------------
# QueryResult — return type for SqlaTable.async_query()
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    """Result of executing a query against a datasource."""

    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    query: str = ""
    status: str = "success"
    error_message: str = ""
    from_dttm: datetime | None = None
    to_dttm: datetime | None = None
    applied_filter_columns: list[str] = field(default_factory=list)
    rejected_filter_columns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------

sqlatable_user = Table(
    "sqlatable_user",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "user_id",
        Integer,
        ForeignKey("ab_user.id", ondelete="CASCADE"),
    ),
    Column(
        "table_id",
        Integer,
        ForeignKey("tables.id", ondelete="CASCADE"),
    ),
)

RLSFilterRoles = Table(
    "rls_filter_roles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "role_id",
        Integer,
        ForeignKey("ab_role.id", ondelete="CASCADE"),
    ),
    Column(
        "rls_filter_id",
        Integer,
        ForeignKey("row_level_security_filters.id", ondelete="CASCADE"),
    ),
)

RLSFilterTables = Table(
    "rls_filter_tables",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "table_id",
        Integer,
        ForeignKey("tables.id", ondelete="CASCADE"),
    ),
    Column(
        "rls_filter_id",
        Integer,
        ForeignKey("row_level_security_filters.id", ondelete="CASCADE"),
    ),
)


# ---------------------------------------------------------------------------
# BaseDatasource mixin
# ---------------------------------------------------------------------------


class BaseDatasource:
    """Common columns shared by all datasource-like models.

    This is an abstract mixin -- not a mapped class itself.
    """

    # Used to do code highlighting when displaying the query in the UI
    query_language: str | None = None

    # Only some datasources support Row Level Security
    is_rls_supported: bool = False

    description = Column(Text)
    default_endpoint = Column(Text)
    is_featured = Column(Boolean, default=False)
    filter_select_enabled = Column(Boolean, default=True)
    offset = Column(Integer, default=0)
    cache_timeout = Column(Integer)
    params = Column(String(1000))
    perm = Column(String(1000))
    schema_perm = Column(String(1000))
    catalog_perm = Column(String(1000))
    is_managed_externally = Column(Boolean, nullable=False, default=False)
    external_url = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# TableColumn
# ---------------------------------------------------------------------------


class TableColumn(AuditMixinNullable, ImportExportMixin, CertificationMixin, Base):
    """A column belonging to a SQL dataset (SqlaTable)."""

    __tablename__ = "table_columns"
    __table_args__ = (UniqueConstraint("table_id", "column_name"),)

    id = Column(Integer, primary_key=True)
    column_name = Column(String(255), nullable=False)
    verbose_name = Column(String(1024))
    is_active = Column(Boolean, default=True)
    type = Column(Text)
    advanced_data_type = Column(String(255))
    groupby = Column(Boolean, default=True)
    filterable = Column(Boolean, default=True)
    description = Column(MediumText())
    table_id = Column(
        Integer,
        ForeignKey("tables.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_dttm = Column(Boolean, default=False)
    expression = Column(MediumText())
    python_date_format = Column(String(255))
    extra = Column(Text)

    # -- relationships --------------------------------------------------------

    table = relationship(
        "SqlaTable",
        foreign_keys=[table_id],
        back_populates="columns",
    )

    # -- Computed properties ---------------------------------------------------

    _NUMERIC_TYPES = frozenset(
        {
            "INT",
            "INTEGER",
            "BIGINT",
            "SMALLINT",
            "TINYINT",
            "FLOAT",
            "DOUBLE",
            "DECIMAL",
            "NUMERIC",
            "REAL",
            "DOUBLE PRECISION",
            "MONEY",
            "NUMBER",
        }
    )

    @property
    def is_numeric(self) -> bool:
        """Check if the column has a numeric datatype."""
        if self.type is None:
            return False
        base = self.type.split("(")[0].strip().upper()
        return base in self._NUMERIC_TYPES

    @property
    def data(self) -> dict[str, Any]:
        """Data representation sent to the frontend."""
        attrs = (
            "advanced_data_type",
            "certification_details",
            "certified_by",
            "column_name",
            "description",
            "expression",
            "filterable",
            "groupby",
            "id",
            "is_dttm",
            "python_date_format",
            "type",
            "verbose_name",
        )
        return {s: getattr(self, s) for s in attrs if hasattr(self, s)}


# ---------------------------------------------------------------------------
# SqlMetric
# ---------------------------------------------------------------------------


class SqlMetric(AuditMixinNullable, ImportExportMixin, CertificationMixin, Base):
    """A metric defined on a SQL dataset."""

    __tablename__ = "sql_metrics"
    __table_args__ = (UniqueConstraint("table_id", "metric_name"),)

    id = Column(Integer, primary_key=True)
    metric_name = Column(String(255), nullable=False)
    verbose_name = Column(String(1024))
    metric_type = Column(String(32))
    description = Column(MediumText())
    d3format = Column(String(128))
    currency = Column(JSON, nullable=True)
    warning_text = Column(Text)
    table_id = Column(
        Integer,
        ForeignKey("tables.id", ondelete="CASCADE"),
        nullable=False,
    )
    expression = Column(MediumText(), nullable=False)
    extra = Column(Text)

    # -- relationships --------------------------------------------------------

    table = relationship(
        "SqlaTable",
        foreign_keys=[table_id],
        back_populates="metrics",
    )

    # -- Computed properties ---------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        """Data representation sent to the frontend."""
        attrs = (
            "certification_details",
            "certified_by",
            "currency",
            "d3format",
            "description",
            "expression",
            "id",
            "metric_name",
            "warning_text",
            "verbose_name",
        )
        return {s: getattr(self, s) for s in attrs}


# ---------------------------------------------------------------------------
# SqlaTable
# ---------------------------------------------------------------------------


class SqlaTable(
    Base, AuditMixinNullable, ImportExportMixin, BaseDatasource, ExploreMixin
):
    """A SQL dataset (table or virtual query)."""

    type = "table"
    query_language = "sql"
    is_rls_supported = True

    __tablename__ = "tables"
    __table_args__ = (
        UniqueConstraint("database_id", "catalog", "schema", "table_name"),
    )

    id = Column(Integer, primary_key=True)
    table_name = Column(String(250))
    main_dttm_col = Column(String(250))
    database_id = Column(Integer, ForeignKey("dbs.id"), nullable=False)
    fetch_values_predicate = Column(String(1000))
    schema = Column(String(255))
    catalog = Column(String(256), nullable=True, default=None)
    sql = Column(MediumText())
    is_sqllab_view = Column(Boolean, default=False)
    template_params = Column(Text)
    extra = Column(Text, default="{}")
    normalize_columns = Column(Boolean, default=False)
    always_filter_main_dttm = Column(Boolean, default=False)
    folders = Column(JSON, nullable=True)

    # -- relationships --------------------------------------------------------

    columns = relationship(
        "TableColumn",
        back_populates="table",
        cascade="all, delete-orphan",
    )
    metrics = relationship(
        "SqlMetric",
        back_populates="table",
        cascade="all, delete-orphan",
    )
    owners = relationship(
        "User",
        secondary=sqlatable_user,
        passive_deletes=True,
    )
    database = relationship(
        "Database",
        foreign_keys=[database_id],
    )

    # -- DatasourceProtocol implementation ------------------------------------

    #: Datasource type identifier used by QueryContext
    type: str = "table"

    #: Aggregation functions mapping for SIMPLE adhoc metrics.
    # Function names are lowercase to match the original SQLAlchemy output
    # (``sa.func.SUM(col)`` compiles to ``sum(col)``), which Cypress tests
    # rely on.
    _SQLA_AGGREGATIONS: dict[str, str] = {
        "COUNT_DISTINCT": "COUNT(DISTINCT {col})",
        "COUNT": "COUNT({col})",
        "SUM": "sum({col})",
        "AVG": "avg({col})",
        "MIN": "min({col})",
        "MAX": "max({col})",
    }

    #: Simple filter operator mapping
    _FILTER_OPS: dict[str, str] = {
        "==": "=",
        "!=": "!=",
        ">": ">",
        "<": "<",
        ">=": ">=",
        "<=": "<=",
        "LIKE": "LIKE",
        "ILIKE": "ILIKE",
        "IS NULL": "IS NULL",
        "IS NOT NULL": "IS NOT NULL",
        "IN": "IN",
        "NOT IN": "NOT IN",
        "EQUALS": "=",
        "NOT_EQUALS": "!=",
        "GREATER_THAN": ">",
        "LESS_THAN": "<",
        "GREATER_THAN_OR_EQUALS": ">=",
        "LESS_THAN_OR_EQUALS": "<=",
    }

    @property
    def uid(self) -> str:
        """Unique identifier for this datasource (id__type)."""
        return f"{self.id}__{self.type}"

    @property
    def column_names(self) -> list[str]:
        """Return a list of column names defined on this dataset."""
        return [c.column_name for c in (self.columns or [])]

    @property
    def datasource_type(self) -> str:
        """Return the datasource type identifier."""
        return self.type

    @property
    def datasource_name(self) -> str:
        """Return the underlying table name."""
        return self.table_name

    @property
    def name(self) -> str:
        """Fully-qualified name: schema.table_name or just table_name."""
        return f"{self.schema}.{self.table_name}" if self.schema else self.table_name

    @property
    def full_name(self) -> str:
        """Fully-qualified name including database and catalog.

        Format: ``[database].[catalog].[schema].[table_name]``, omitting
        empty parts, matching the original
        ``utils.get_datasource_full_name()`` behaviour.
        """
        parts: list[str] = []
        if not sa.inspect(self).unloaded.intersection({"database"}):
            db = self.database
            if db is not None:
                parts.append(f"[{db}]")
        if self.catalog:
            parts.append(f"[{self.catalog}]")
        if self.schema:
            parts.append(f"[{self.schema}]")
        parts.append(f"[{self.table_name}]")
        return ".".join(parts)

    @property
    def select_star(self) -> str | None:
        """Generate a SELECT * query for this table.

        Matches original SqlaTable.select_star (line 1331-1338):
        calls Database.select_star with show_cols=False and
        latest_partition=False to avoid expensive DB inspection.
        """
        if sa.inspect(self).unloaded.intersection({"database"}):
            return None
        if self.database is None:
            return None

        from superset.db_engine_specs.base import BaseEngineSpec

        table = Table(
            str(self.table_name),
            self.schema or None,
            self.catalog or None,
        )

        try:
            return BaseEngineSpec.select_star(
                database=self.database,
                table=table,
                engine=None,  # Not needed when indent=False
                limit=100,
                show_cols=False,
                indent=False,
                latest_partition=False,
            )
        except Exception:
            logger.warning(
                "Failed to generate select_star for table '%s'",
                self.table_name,
                exc_info=True,
            )
            return None

    def external_metadata(self) -> list[dict[str, Any]]:
        """Fetch column metadata from the underlying database.

        Matches original SqlaTable.external_metadata (line 1313-1321):
        - Virtual datasets (with custom SQL) -> get_virtual_table_metadata
        - Physical tables -> get_physical_table_metadata
        """

        if self.sql:
            return self._get_virtual_table_metadata()
        return self._get_physical_table_metadata()

    def _get_virtual_table_metadata(self) -> list[dict[str, Any]]:
        """Get column metadata for a virtual dataset (custom SQL query).

        Executes the SQL with a LIMIT 0 to get column names and types
        from the result set metadata.
        """
        if not self.sql:
            return []

        # Strip trailing semicolon and wrap in a subquery
        inner_sql = self.sql.strip().rstrip(";")
        metadata_sql = f"SELECT * FROM ({inner_sql}) AS virtual_table LIMIT 0"  # noqa: S608

        try:
            from sqlalchemy import text as sa_text

            from superset.utils.database import get_sync_connection

            with get_sync_connection(self.database) as (conn, spec):
                result = conn.execute(sa_text(metadata_sql))
                columns: list[dict[str, Any]] = []
                for col in result.cursor.description or []:
                    columns.append(
                        {
                            "column_name": col[0],
                            "type": col[1] if len(col) > 1 else None,
                        }
                    )
                return columns
        except Exception:
            logger.warning(
                "Failed to get virtual table metadata for '%s'",
                self.table_name,
                exc_info=True,
            )
            return []

    def _get_physical_table_metadata(self) -> list[dict[str, Any]]:
        """Get column metadata for a physical table.

        Uses the database engine spec's get_columns method to fetch
        metadata from the actual database table.
        """

        table = Table(
            str(self.table_name),
            self.schema or None,
            self.catalog or None,
        )

        try:
            from superset.utils.database import get_sync_connection

            with get_sync_connection(self.database) as (conn, spec):
                return spec.get_columns(
                    database=self.database,
                    table=table,
                    conn=conn,
                    schema=self.schema,
                )
        except Exception:
            logger.warning(
                "Failed to get physical table metadata for '%s'",
                self.table_name,
                exc_info=True,
            )
            return []

    def get_perm(self) -> str:
        """Return this dataset's permission name.

        Format: ``[database].[table_name](id:N)``
        """
        if sa.inspect(self).unloaded.intersection({"database"}):
            return self.perm or ""
        if self.database is None:
            raise ValueError("Cannot evaluate permission: database is None")
        return f"[{self.database}].[{self.table_name}](id:{self.id})"

    def get_schema_perm(self) -> str | None:
        """Return schema permission string: ``[database].[schema]``."""
        if sa.inspect(self).unloaded.intersection({"database"}):
            return self.schema_perm
        if self.database is None or not self.schema:
            return None
        db_name = getattr(self.database, "database_name", str(self.database))
        return f"[{db_name}].[{self.schema}]"

    def get_catalog_perm(self) -> str | None:
        """Return catalog permission string: ``[database].[catalog]``."""
        if sa.inspect(self).unloaded.intersection({"database"}):
            return self.catalog_perm
        if self.database is None or not self.catalog:
            return None
        db_name = getattr(self.database, "database_name", str(self.database))
        return f"[{db_name}].[{self.catalog}]"

    @property
    def columns_dict(self) -> dict[str, TableColumn]:
        """Map of column_name -> TableColumn."""
        return {c.column_name: c for c in (self.columns or [])}

    @property
    def metrics_dict(self) -> dict[str, SqlMetric]:
        """Map of metric_name -> SqlMetric."""
        return {m.metric_name: m for m in (self.metrics or [])}

    @property
    def dttm_cols(self) -> list[str]:
        """Return column names marked as datetime.

        Matches original: includes main_dttm_col even if not marked is_dttm.
        """
        cols = [c.column_name for c in (self.columns or []) if c.is_dttm]
        if self.main_dttm_col and self.main_dttm_col not in cols:
            cols.append(self.main_dttm_col)
        return cols

    @property
    def any_dttm_col(self) -> str | None:
        """Return the first datetime column name, or None."""
        cols = self.dttm_cols
        return cols[0] if cols else None

    @property
    def num_cols(self) -> list[str]:
        """Return column names with numeric types."""
        return [c.column_name for c in (self.columns or []) if c.is_numeric]

    @property
    def column_formats(self) -> dict[str, str | None]:
        """Map of metric_name -> d3format for metrics that define one."""
        return {m.metric_name: m.d3format for m in (self.metrics or []) if m.d3format}

    @property
    def verbose_map(self) -> dict[str, str]:
        """Map identifiers to verbose names for display."""
        verb_map: dict[str, str] = {"__timestamp": "Time"}
        for m in self.metrics or []:
            if m.metric_name not in verb_map:
                verb_map[m.metric_name] = m.verbose_name or m.metric_name
        for c in self.columns or []:
            if c.column_name not in verb_map:
                verb_map[c.column_name] = c.verbose_name or c.column_name
        return verb_map

    @property
    def data(self) -> dict[str, Any]:
        """Full data representation of this datasource sent to the frontend."""
        db_data: dict[str, Any] = {}
        if not sa.inspect(self).unloaded.intersection({"database"}):
            db = self.database
            if db is not None and hasattr(db, "data"):
                db_data = db.data

        data_: dict[str, Any] = {
            # simple fields
            "id": self.id,
            "uid": self.uid,
            "column_formats": self.column_formats,
            "description": self.description,
            "database": db_data,
            "default_endpoint": self.default_endpoint,
            "filter_select": self.filter_select_enabled,
            "filter_select_enabled": self.filter_select_enabled,
            "name": self.name,
            "datasource_name": self.datasource_name,
            "table_name": self.datasource_name,
            "type": self.type,
            "catalog": self.catalog,
            "schema": self.schema or None,
            "offset": self.offset,
            "cache_timeout": self.cache_timeout,
            "params": self.params,
            "perm": self.perm,
            "edit_url": f"/tablemodelview/edit/{self.id}",
            # sqla-specific
            "sql": self.sql,
            # one to many
            "columns": [o.data for o in (self.columns or [])],
            "metrics": [o.data for o in (self.metrics or [])],
            "folders": self.folders,
            "order_by_choices": [],
            "owners": [owner.id for owner in (self.owners or [])],
            "verbose_map": self.verbose_map,
            "select_star": self.select_star,
        }

        # SqlaTable-specific extensions (matches original .data property)
        data_["granularity_sqla"] = [(c, c) for c in self.dttm_cols]
        data_["time_grain_sqla"] = []
        data_["main_dttm_col"] = self.main_dttm_col
        data_["fetch_values_predicate"] = self.fetch_values_predicate
        data_["template_params"] = self.template_params
        data_["is_sqllab_view"] = self.is_sqllab_view
        data_["health_check_message"] = None
        data_["extra"] = self.extra
        data_["owners"] = [
            {
                "first_name": getattr(o, "first_name", ""),
                "last_name": getattr(o, "last_name", ""),
                "username": getattr(o, "username", ""),
                "id": o.id,
            }
            for o in (self.owners or [])
        ]
        data_["always_filter_main_dttm"] = self.always_filter_main_dttm
        data_["normalize_columns"] = self.normalize_columns
        return data_

    @property
    def _backend(self) -> str:
        """Extract database backend name from the sqlalchemy_uri."""
        uri = getattr(self.database, "sqlalchemy_uri", "")
        if "://" in uri:
            return uri.split("://")[0].split("+")[0]
        return "postgresql"

    def _quote_identifier(self, name: str) -> str:
        """Quote an identifier using engine-appropriate quoting.

        MySQL uses backticks, MSSQL uses brackets, everything else
        uses standard double-quotes.
        """
        uri = getattr(self.database, "sqlalchemy_uri", "") or ""
        uri_lower = uri.lower()
        if "mysql" in uri_lower:
            return f"`{name}`"
        if "mssql" in uri_lower:
            return f"[{name}]"
        return f'"{name}"'

    def _quote_col_if_needed(self, name: str) -> str:
        """Quote a column reference only when the identifier actually
        requires quoting (mirrors SQLAlchemy's default behaviour for
        ``column()`` / ``literal_column()``).

        An identifier is returned unquoted when it consists solely of
        lowercase ASCII letters, digits, and underscores, does not start
        with a digit, and is not on the list of SQL reserved keywords.
        Everything else is passed through :meth:`_quote_identifier`.
        """
        if not name:
            return name
        if not (name[0].isalpha() or name[0] == "_"):
            return self._quote_identifier(name)
        for ch in name:
            if not (ch.isdigit() or ch == "_" or ("a" <= ch <= "z")):
                return self._quote_identifier(name)
        if name in _SQL_RESERVED_WORDS:
            return self._quote_identifier(name)
        return name

    def get_column(self, column_name: str | None) -> TableColumn | None:
        """Retrieve a TableColumn by name, or None."""
        if column_name is None:
            return None
        for col in self.columns or []:
            if col.column_name == column_name:
                return col
        return None

    def get_extra_cache_keys(self, query_dict: dict[str, Any]) -> list[str]:
        """Return extra cache keys for per-query cache isolation."""
        return []

    def clone(self) -> SqlaTable:
        """Create a copy of this dataset.

        Ported from superset_old/commands/dataset/duplicate.py.
        Copies key fields and deep-copies columns and metrics into new
        instances (without IDs) so the clone can be persisted independently.
        """
        table = SqlaTable(
            table_name=self.table_name,
            database_id=self.database_id,
            schema=self.schema,
            catalog=self.catalog,
            sql=self.sql,
            is_sqllab_view=self.is_sqllab_view,
            template_params=self.template_params,
            normalize_columns=self.normalize_columns,
            always_filter_main_dttm=self.always_filter_main_dttm,
            main_dttm_col=self.main_dttm_col,
            fetch_values_predicate=self.fetch_values_predicate,
            extra=self.extra,
            description=self.description,
            default_endpoint=self.default_endpoint,
            offset=self.offset,
            cache_timeout=self.cache_timeout,
            params=self.params,
            filter_select_enabled=self.filter_select_enabled,
            folders=self.folders,
        )

        # Deep-copy columns
        cols: list[TableColumn] = []
        for c in self.columns or []:
            cols.append(
                TableColumn(
                    column_name=c.column_name,
                    verbose_name=c.verbose_name,
                    expression=c.expression,
                    filterable=c.filterable,
                    groupby=c.groupby,
                    is_dttm=c.is_dttm,
                    type=c.type,
                    description=c.description,
                    is_active=c.is_active,
                    advanced_data_type=c.advanced_data_type,
                    python_date_format=c.python_date_format,
                    extra=c.extra,
                )
            )
        table.columns = cols

        # Deep-copy metrics
        mets: list[SqlMetric] = []
        for m in self.metrics or []:
            mets.append(
                SqlMetric(
                    metric_name=m.metric_name,
                    verbose_name=m.verbose_name,
                    expression=m.expression,
                    metric_type=m.metric_type,
                    description=m.description,
                    d3format=m.d3format,
                    currency=m.currency,
                    warning_text=m.warning_text,
                    extra=m.extra,
                )
            )
        table.metrics = mets

        return table

    def to_dict(self) -> dict[str, Any]:
        """Serialize this dataset to a plain dictionary.

        Returns a dict of the key model fields suitable for JSON
        serialization or comparison.  Does NOT include relationships
        (columns, metrics, owners) to avoid lazy-load side-effects;
        use the ``data`` property for the full frontend payload.
        """
        return {
            "id": self.id,
            "table_name": self.table_name,
            "database_id": self.database_id,
            "schema": self.schema,
            "catalog": self.catalog,
            "sql": self.sql,
            "is_sqllab_view": self.is_sqllab_view,
            "template_params": self.template_params,
            "main_dttm_col": self.main_dttm_col,
            "fetch_values_predicate": self.fetch_values_predicate,
            "description": self.description,
            "default_endpoint": self.default_endpoint,
            "offset": self.offset,
            "cache_timeout": self.cache_timeout,
            "params": self.params,
            "perm": self.perm,
            "schema_perm": self.schema_perm,
            "catalog_perm": self.catalog_perm,
            "filter_select_enabled": self.filter_select_enabled,
            "extra": self.extra,
            "normalize_columns": self.normalize_columns,
            "always_filter_main_dttm": self.always_filter_main_dttm,
            "folders": self.folders,
            "is_managed_externally": self.is_managed_externally,
            "external_url": self.external_url,
        }

    # -- SQL generation -------------------------------------------------------

    def _get_table_ref(self) -> str:
        """Return fully-qualified table reference for FROM clause."""
        if self.sql:
            # Virtual dataset — wrap the custom SQL as a subquery
            inner = self.sql.strip().rstrip(";")
            return f"({inner}) AS virtual_table"
        parts: list[str] = []
        if self.catalog:
            parts.append(self._quote_identifier(str(self.catalog)))
        if self.schema:
            parts.append(self._quote_identifier(str(self.schema)))
        parts.append(self._quote_identifier(str(self.table_name)))
        return ".".join(parts)

    def _resolve_metric_expression(self, metric: Any) -> tuple[str, str]:
        """Resolve a metric to (sql_expression, label).

        Handles three forms:
        1. String metric name — look up in SqlMetric definitions
        2. Adhoc SIMPLE metric — {expressionType: "SIMPLE", column: {...}}
        3. Adhoc SQL metric — {expressionType: "SQL", sqlExpression: "..."}

        Returns (expression_sql, label).
        """
        if isinstance(metric, str):
            # Named metric — look up in the dataset's metric definitions
            metrics_by_name = {m.metric_name: m for m in (self.metrics or [])}
            if metric in metrics_by_name:
                m = metrics_by_name[metric]
                return m.expression, metric
            raise ValueError(f"Metric not found: {metric}")

        if isinstance(metric, dict):
            label = (
                metric.get("label")
                or metric.get("optionName")
                or metric.get("option_name")
                or str(metric)
            )
            expr_type = (
                metric.get("expressionType") or metric.get("expression_type") or ""
            )

            if expr_type == "SIMPLE":
                col_obj = metric.get("column") or {}
                col_name = col_obj.get("column_name", "*")
                # If the column has an expression (calculated/virtual column),
                # use it instead of the physical column name.
                # Matches original adhoc_metric_to_sqla + make_sqla_column logic.
                col_expression = col_obj.get("expression")
                aggregate = metric.get("aggregate", "COUNT")
                tmpl = self._SQLA_AGGREGATIONS.get(
                    aggregate, f"{aggregate.lower()}({{col}})"
                )
                if col_expression:
                    col_ref = col_expression
                elif col_name != "*":
                    # Use SQLAlchemy-style conditional quoting so simple
                    # lowercase identifiers like ``num_girls`` remain
                    # unquoted; matches original ``sa.func.SUM(column(...))``
                    # compilation output.
                    col_ref = self._quote_col_if_needed(col_name)
                else:
                    col_ref = col_name
                return tmpl.format(col=col_ref), label

            if expr_type == "SQL":
                sql_expr = (
                    metric.get("sqlExpression")
                    or metric.get("sql_expression")
                    or "COUNT(*)"
                )
                return sql_expr, label

        # Fallback
        return "COUNT(*)", str(metric)

    def _resolve_column_expression(self, col: Any) -> tuple[str, str]:
        """Resolve a column spec to (sql_expression, label).

        Handles:
        1. String column name
        2. Adhoc column dict with sqlExpression and label
        """
        if isinstance(col, str):
            # Check if column has a custom expression
            col_obj = self.get_column(col)
            if col_obj and col_obj.expression:
                return col_obj.expression, col
            return self._quote_identifier(col), col

        if isinstance(col, dict):
            sql_expr = col.get("sqlExpression") or col.get("sql_expression")
            label = col.get("label") or sql_expr or ""
            if sql_expr:
                return sql_expr, label
            col_name = col.get("column_name") or label or ""
            return self._quote_identifier(str(col_name)), label

        return str(col), str(col)

    def _get_time_grain_expr(self, col_name: str, time_grain: str | None) -> str:
        """Apply time grain truncation to a column using engine-specific expressions."""
        from superset.utils.database import get_engine_spec_for_database

        spec = get_engine_spec_for_database(self.database)
        grain_exprs = spec.get_time_grain_expressions()
        col_ref = self._quote_identifier(col_name)

        if time_grain and time_grain in grain_exprs:
            return grain_exprs[time_grain].replace("{col}", col_ref)
        if None in grain_exprs:
            return grain_exprs[None].replace("{col}", col_ref)
        return col_ref

    def _build_filter_clause(  # noqa: C901
        self,
        flt: dict[str, Any],
        from_dttm: datetime | str | None,
        to_dttm: datetime | str | None,
    ) -> str | None:
        """Convert a single filter dict to a SQL WHERE fragment."""
        col = flt.get("col")
        op = flt.get("op", "")
        val = flt.get("val")

        if not col or not op:
            return None

        op_upper = op.upper().strip()

        # Mirror the original ``BaseDatasource._get_sqla_query`` behaviour:
        # if the filter references a column that doesn't exist on this
        # datasource (and isn't an adhoc/SQL expression), silently drop
        # the filter rather than generating SQL that the database will
        # reject. The column is recorded under ``rejected_filter_columns``
        # in the response payload by ``query_context_processor``. Only
        # plain column-name strings are validated; dicts (adhoc columns)
        # are passed through unchanged.
        #
        # This check runs BEFORE TEMPORAL_RANGE handling because a
        # TEMPORAL_RANGE filter referencing a non-existent datetime
        # column would still generate SQL like ``"ds" >= ...`` that
        # fails at execute. Cypress tests override datasources (e.g.
        # graph test targets energy_usage which has no ``ds`` column)
        # and rely on stale temporal filters being silently dropped.
        if isinstance(col, str):
            known_cols = {c.column_name for c in (self.columns or [])}
            if known_cols and col not in known_cols:
                return None

        # Handle TEMPORAL_RANGE filter
        if op_upper == "TEMPORAL_RANGE" and isinstance(val, str):
            return self._build_temporal_range_filter(col, val)

        qcol = self._quote_identifier(col)

        # Handle IS NULL / IS NOT NULL
        if op_upper in ("IS NULL", "IS_NULL"):
            return f"{qcol} IS NULL"
        if op_upper in ("IS NOT NULL", "IS_NOT_NULL"):
            return f"{qcol} IS NOT NULL"

        # Handle IS TRUE / IS FALSE
        if op_upper in ("IS TRUE", "IS_TRUE"):
            return f"{qcol} IS TRUE"
        if op_upper in ("IS FALSE", "IS_FALSE"):
            return f"{qcol} IS FALSE"

        # Handle IN / NOT IN
        if op_upper in ("IN", "NOT IN", "NOT_IN"):
            values = val if isinstance(val, list) else [val]
            if not values:
                return None
            escaped = []
            for v in values:
                if v is None:
                    escaped.append("NULL")
                elif isinstance(v, (int, float)):
                    escaped.append(str(v))
                else:
                    escaped.append(f"'{_escape_sql_string(str(v))}'")
            in_list = ", ".join(escaped)
            negation = "NOT " if "NOT" in op_upper else ""
            return f"{qcol} {negation}IN ({in_list})"

        # Handle LIKE / ILIKE / NOT LIKE
        if op_upper in ("LIKE", "ILIKE", "NOT_LIKE", "NOT LIKE"):
            escaped_val = _escape_sql_string(str(val)) if val is not None else ""
            sql_op = op_upper.replace("_", " ")
            return f"{qcol} {sql_op} '{escaped_val}'"

        # Handle comparison operators
        sql_op = self._FILTER_OPS.get(op_upper, op_upper)
        if val is None:
            if sql_op in ("=", "EQUALS"):
                return f"{qcol} IS NULL"
            if sql_op in ("!=", "NOT_EQUALS"):
                return f"{qcol} IS NOT NULL"
            return None

        if isinstance(val, (int, float)):
            return f"{qcol} {sql_op} {val}"
        return f"{qcol} {sql_op} '{_escape_sql_string(str(val))}'"

    def _build_temporal_range_filter(self, col: str, time_range: str) -> str | None:
        """Build a WHERE clause from a TEMPORAL_RANGE filter value.

        Uses the full get_since_until() parser which handles ISO dates,
        relative expressions like "7 days ago", and complex expressions
        like "DATETRUNC(DATETIME('today'), WEEK)".
        """
        from superset.utils.date import get_since_until

        if not time_range or time_range.lower() == "no filter":
            return None

        try:
            since_dt, until_dt = get_since_until(time_range=time_range)
        except (ValueError, Exception):
            logger.warning(
                "Failed to parse temporal range '%s' for col '%s'",
                time_range,
                col,
                exc_info=True,
            )
            return None

        qcol = self._quote_identifier(col)
        clauses: list[str] = []
        if since_dt:
            clauses.append(f"{qcol} >= '{since_dt.isoformat()}'")
        if until_dt:
            clauses.append(f"{qcol} < '{until_dt.isoformat()}'")

        return " AND ".join(clauses) if clauses else None

    def _build_sql(  # noqa: C901
        self,
        query_dict: dict[str, Any],
        rls_filters: list[str] | None = None,
    ) -> tuple[str, datetime | None, datetime | None]:
        """Build a SQL string from query_dict parameters.

        Args:
            query_dict: The query parameters dict from QueryContext.
            rls_filters: Optional list of raw SQL WHERE-clause fragments
                from Row-Level Security rules. The caller
                (QueryContextProcessor) is responsible for fetching
                these from the security manager.

        Returns (sql, from_dttm, to_dttm).
        """
        columns_raw: list[Any] = query_dict.get("columns", [])
        metrics_raw: list[Any] = query_dict.get("metrics", [])
        groupby_raw: list[Any] = query_dict.get("groupby", [])
        filters: list[dict[str, Any]] = query_dict.get("filter", [])
        extras: dict[str, Any] = query_dict.get("extras", {})
        granularity: str | None = query_dict.get("granularity")
        from_dttm = query_dict.get("from_dttm")
        to_dttm = query_dict.get("to_dttm")
        order_desc: bool = query_dict.get("order_desc", True)
        orderby: list[Any] = query_dict.get("orderby", [])
        row_limit: int | None = query_dict.get("row_limit")
        row_offset: int = query_dict.get("row_offset", 0)
        is_timeseries: bool = query_dict.get("is_timeseries", False)
        time_grain: str | None = extras.get("time_grain_sqla")
        series_limit: int | None = query_dict.get("series_limit")
        series_limit_metric: Any = query_dict.get("series_limit_metric")

        # Fallback: if granularity column doesn't exist in this
        # dataset's datetime columns, use main_dttm_col instead.
        # Matches original get_sqla_query() logic in helpers.py:1683-1684.
        if granularity is not None and granularity not in self.dttm_cols:
            granularity = self.main_dttm_col

        # Parse datetime bounds
        from_dttm = _parse_dttm(from_dttm)
        to_dttm = _parse_dttm(to_dttm)

        # Determine if we need aggregation.  Matches original
        # ``helpers.get_sqla_query`` logic (lines 1814-1822): we start with
        # ``need_groupby = bool(metrics or groupby)`` and then flip it to
        # ``True`` if ``orderby`` references any metric — adhoc or named —
        # because in that case the query must aggregate even when the form
        # has no metrics selected (e.g. table viz in raw mode ordering by
        # ``SUM(num) DESC`` with only ``columns=['name']``).
        need_groupby = bool(metrics_raw or groupby_raw)
        if not need_groupby and orderby:
            named_metrics = {m.metric_name for m in (self.metrics or [])}
            for item in orderby:
                if not (isinstance(item, (list, tuple)) and len(item) == 2):
                    continue
                col_spec = item[0]
                if isinstance(col_spec, dict):
                    # Adhoc metric dict — mirror original's need_groupby flip
                    need_groupby = True
                    break
                if isinstance(col_spec, str) and col_spec in named_metrics:
                    need_groupby = True
                    break

        # ----- SELECT clause -----
        select_parts: list[str] = []
        group_by_parts: list[str] = []
        labels_expected: list[str] = []

        # If granularity is set and it's a timeseries, add time column first
        if granularity and is_timeseries:
            time_expr = self._get_time_grain_expr(granularity, time_grain)
            alias = "__timestamp"
            select_parts.append(f"{time_expr} AS {self._quote_identifier(alias)}")
            group_by_parts.append(time_expr)
            labels_expected.append(alias)

        if need_groupby:
            # GROUP BY mode: resolve groupby/columns, then metrics
            groupby_cols = groupby_raw or columns_raw
            for col_spec in groupby_cols:
                expr, label = self._resolve_column_expression(col_spec)

                # If this column IS the granularity and we have a time grain,
                # apply time grain truncation
                col_name = (
                    col_spec
                    if isinstance(col_spec, str)
                    else (
                        col_spec.get("label") or col_spec.get("column_name", "")
                        if isinstance(col_spec, dict)
                        else str(col_spec)
                    )
                )
                if col_name == granularity and time_grain:
                    expr = self._get_time_grain_expr(col_name, time_grain)

                if label not in labels_expected:
                    select_parts.append(f"{expr} AS {self._quote_identifier(label)}")
                    group_by_parts.append(expr)
                    labels_expected.append(label)

            for metric in metrics_raw:
                expr, label = self._resolve_metric_expression(metric)
                select_parts.append(f"{expr} AS {self._quote_identifier(label)}")
                labels_expected.append(label)
        else:
            # Raw columns mode (no aggregation)
            for col_spec in columns_raw:
                expr, label = self._resolve_column_expression(col_spec)
                select_parts.append(f"{expr} AS {self._quote_identifier(label)}")
                labels_expected.append(label)

        if not select_parts:
            select_parts.append("*")

        # ----- FROM clause -----
        table_ref = self._get_table_ref()

        # ----- WHERE clause -----
        where_parts: list[str] = []

        # Time filter from from_dttm / to_dttm
        if granularity and from_dttm:
            col_obj = self.get_column(granularity)
            col_ref = self._quote_identifier(granularity)
            if col_obj and col_obj.expression:
                col_ref = str(col_obj.expression)
            where_parts.append(f"{col_ref} >= '{from_dttm.isoformat()}'")
        if granularity and to_dttm:
            col_obj = self.get_column(granularity)
            col_ref = self._quote_identifier(granularity)
            if col_obj and col_obj.expression:
                col_ref = str(col_obj.expression)
            where_parts.append(f"{col_ref} < '{to_dttm.isoformat()}'")

        # Adhoc filters
        for flt in filters:
            clause = self._build_filter_clause(flt, from_dttm, to_dttm)
            if clause:
                where_parts.append(clause)

        # extras.where
        if extras.get("where"):
            where_parts.append(f"({extras['where']})")

        # Row-Level Security filters
        if rls_filters:
            for rls_clause in rls_filters:
                rls_clause = rls_clause.strip()
                if rls_clause:
                    where_parts.append(f"({rls_clause})")

        # ----- HAVING clause -----
        having_parts: list[str] = []
        if extras.get("having"):
            having_parts.append(f"({extras['having']})")

        # ----- ORDER BY clause -----
        order_parts: list[str] = []
        if orderby:
            metrics_by_name = {m.metric_name: m for m in (self.metrics or [])}
            columns_by_name = {c.column_name: c for c in (self.columns or [])}
            for item in orderby:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    col_spec, ascending = item
                    # Resolve the column/metric for ordering.
                    # Original logic (helpers.py:1815-1826): checks
                    # metrics_exprs_by_label → metrics_by_name → columns_by_name
                    if isinstance(col_spec, str):
                        if col_spec in metrics_by_name:
                            # Named metric — use its stored expression
                            order_ref = metrics_by_name[col_spec].expression
                        elif col_spec in columns_by_name:
                            col_obj = columns_by_name[col_spec]
                            order_ref = (
                                col_obj.expression
                                if col_obj.expression
                                else self._quote_identifier(col_spec)
                            )
                        else:
                            # Could be a label alias from the SELECT list
                            order_ref = self._quote_identifier(col_spec)
                    elif isinstance(col_spec, dict):
                        expr, _label = self._resolve_metric_expression(col_spec)
                        order_ref = expr
                    else:
                        order_ref = str(col_spec)
                    direction = "ASC" if ascending else "DESC"
                    order_parts.append(f"{order_ref} {direction}")
        elif metrics_raw and need_groupby:
            # Default: order by first metric
            first_metric_label = (
                labels_expected[-len(metrics_raw)] if metrics_raw else None
            )
            if first_metric_label:
                direction = "DESC" if order_desc else "ASC"
                order_parts.append(
                    f"{self._quote_identifier(first_metric_label)} {direction}"
                )

        # ----- Series limit subquery -----
        # When series_limit is set, restrict the outer query to only the
        # top N series.  We build a subquery that groups by the series
        # columns (groupby minus the time column), orders by the
        # series_limit_metric (or the first metric), and limits to N.
        # The outer query then filters via WHERE … IN (subquery).
        series_limit_clause: str | None = None
        if series_limit and need_groupby and group_by_parts:
            # Determine which group-by expressions represent the series
            # (everything except the time grain column).
            series_group_exprs: list[str] = []
            series_group_labels: list[str] = []
            groupby_cols_list = groupby_raw or columns_raw
            for col_spec in groupby_cols_list:
                col_name = (
                    col_spec
                    if isinstance(col_spec, str)
                    else (
                        col_spec.get("label") or col_spec.get("column_name", "")
                        if isinstance(col_spec, dict)
                        else str(col_spec)
                    )
                )
                # Skip the granularity column — it is not a series dimension
                if col_name == granularity and is_timeseries:
                    continue
                expr, label = self._resolve_column_expression(col_spec)
                series_group_exprs.append(expr)
                series_group_labels.append(label)

            if series_group_exprs:
                # Determine the ordering metric for the subquery
                if series_limit_metric:
                    sl_expr, _sl_label = self._resolve_metric_expression(
                        series_limit_metric
                    )
                elif metrics_raw:
                    sl_expr, _sl_label = self._resolve_metric_expression(metrics_raw[0])
                else:
                    sl_expr = "COUNT(*)"

                direction = "DESC" if order_desc else "ASC"
                inner_select = ", ".join(series_group_exprs)
                inner_group = ", ".join(series_group_exprs)
                inner_where = (
                    f"\nWHERE {' AND '.join(where_parts)}" if where_parts else ""
                )
                subq = (
                    f"SELECT {inner_select}\n"
                    f"FROM {table_ref}{inner_where}\n"
                    f"GROUP BY {inner_group}\n"
                    f"ORDER BY {sl_expr} {direction}\n"
                    f"LIMIT {int(series_limit)}"
                )

                if len(series_group_exprs) == 1:
                    series_limit_clause = f"{series_group_exprs[0]} IN ({subq})"
                else:
                    # Multi-column series: use a tuple IN subquery
                    outer_tuple = ", ".join(series_group_exprs)
                    series_limit_clause = f"({outer_tuple}) IN ({subq})"

        # ----- Assemble SQL -----
        sql = f"SELECT {', '.join(select_parts)}\nFROM {table_ref}"

        # Merge series limit into WHERE
        all_where = list(where_parts)
        if series_limit_clause:
            all_where.append(series_limit_clause)

        if all_where:
            sql += f"\nWHERE {' AND '.join(all_where)}"

        if group_by_parts and need_groupby:
            sql += f"\nGROUP BY {', '.join(group_by_parts)}"

        if having_parts:
            sql += f"\nHAVING {' AND '.join(having_parts)}"

        if order_parts:
            sql += f"\nORDER BY {', '.join(order_parts)}"

        if row_limit:
            sql += f"\nLIMIT {int(row_limit)}"

        if row_offset:
            sql += f"\nOFFSET {int(row_offset)}"

        return sql, from_dttm, to_dttm

    # -- Async query execution ------------------------------------------------

    async def async_values_for_column(
        self,
        column_name: str,
        limit: int = 10000,
    ) -> list[Any]:
        """Return distinct values of ``column_name`` for filter dropdowns.

        Async port of ``superset_old.models.helpers.values_for_column``.
        Builds ``SELECT DISTINCT <col> AS column_values FROM <table>
        [WHERE <fetch_values_predicate>] LIMIT <n>`` and executes it via
        the dataset's async engine.
        """
        cols = {c.column_name: c for c in (self.columns or [])}
        if column_name not in cols:
            raise KeyError(column_name)

        target_col = cols[column_name]

        # Respect calculated columns: use ``expression`` when present,
        # otherwise quote the physical column name.
        col_expr = getattr(target_col, "expression", None)
        if col_expr:
            projection = f"{col_expr} AS column_values"
        else:
            projection = f"{self._quote_identifier(column_name)} AS column_values"

        table_ref = self._get_table_ref()
        sql = f"SELECT DISTINCT {projection} FROM {table_ref}"

        fvp = getattr(self, "fetch_values_predicate", None)
        if fvp:
            sql += f" WHERE {fvp}"

        if limit:
            sql += f" LIMIT {int(limit)}"

        df = await self._execute_sql(sql)
        if df.empty or "column_values" not in df.columns:
            return []
        values = df["column_values"].replace({np.nan: None}).tolist()
        return values

    async def _execute_sql(self, sql: str) -> pd.DataFrame:
        """Execute SQL against the dataset's database and return a DataFrame."""
        from sqlalchemy.sql import text as sa_text

        from superset.utils.database import get_async_connection

        async with get_async_connection(self.database) as (conn, _spec):
            result = await conn.execute(sa_text(sql))
            if result.returns_rows:
                cols = list(result.keys())
                rows = result.fetchall()
                return pd.DataFrame([tuple(row) for row in rows], columns=cols)
            return pd.DataFrame()

    async def async_query(
        self,
        query_dict: dict[str, Any],
        rls_filters: list[str] | None = None,
    ) -> QueryResult:
        """Execute a query against this dataset and return a QueryResult.

        This is the primary entry point for the async query pipeline used by
        AsyncQueryContextProcessor._get_query_result().

        Args:
            query_dict: The query parameters dict from QueryContext.
            rls_filters: Optional list of raw SQL WHERE-clause fragments
                from Row-Level Security rules.  The caller is responsible
                for obtaining these from the security manager.
        """
        try:
            sql, from_dttm, to_dttm = self._build_sql(
                query_dict, rls_filters=rls_filters
            )
            logger.debug("SqlaTable.async_query SQL:\n%s", sql)
            df = await self._execute_sql(sql)
            return QueryResult(
                df=df,
                query=sql,
                status="success",
                from_dttm=from_dttm,
                to_dttm=to_dttm,
            )
        except Exception as ex:
            logger.warning(
                "async_query failed for table %s: %s",
                self.table_name,
                ex,
                exc_info=True,
            )
            return QueryResult(
                df=pd.DataFrame(),
                query="",
                status="error",
                error_message=str(ex),
            )


# ---------------------------------------------------------------------------
# RowLevelSecurityFilter
# ---------------------------------------------------------------------------


class RowLevelSecurityFilter(Base, AuditMixinNullable):
    """A row-level security filter applied to datasets."""

    __tablename__ = "row_level_security_filters"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True)
    description = Column(Text)
    filter_type = Column(String(50))
    group_key = Column(String(255))
    clause = Column(MediumText(), nullable=False)

    # -- relationships --------------------------------------------------------

    roles = relationship(
        "Role",
        secondary=RLSFilterRoles,
    )
    tables = relationship(
        "SqlaTable",
        secondary=RLSFilterTables,
    )
