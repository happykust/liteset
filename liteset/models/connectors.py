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
"""Connector models: SqlaTable, TableColumn, SqlMetric, RowLevelSecurityFilter.

Pure SQLAlchemy -- no Flask dependencies.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import relationship

from liteset.models.helpers import (
    AuditMixinNullable,
    Base,
    CertificationMixin,
    ImportExportMixin,
    MediumText,
    metadata,
)


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
        ForeignKey(
            "row_level_security_filters.id", ondelete="CASCADE"
        ),
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
        ForeignKey(
            "row_level_security_filters.id", ondelete="CASCADE"
        ),
    ),
)


# ---------------------------------------------------------------------------
# BaseDatasource mixin
# ---------------------------------------------------------------------------


class BaseDatasource:
    """Common columns shared by all datasource-like models.

    This is an abstract mixin -- not a mapped class itself.
    """

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
    is_managed_externally = Column(
        Boolean, nullable=False, default=False
    )
    external_url = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# TableColumn
# ---------------------------------------------------------------------------


class TableColumn(
    AuditMixinNullable, ImportExportMixin, CertificationMixin, Base
):
    """A column belonging to a SQL dataset (SqlaTable)."""

    __tablename__ = "table_columns"
    __table_args__ = (
        UniqueConstraint("table_id", "column_name"),
    )

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


# ---------------------------------------------------------------------------
# SqlMetric
# ---------------------------------------------------------------------------


class SqlMetric(
    AuditMixinNullable, ImportExportMixin, CertificationMixin, Base
):
    """A metric defined on a SQL dataset."""

    __tablename__ = "sql_metrics"
    __table_args__ = (
        UniqueConstraint("table_id", "metric_name"),
    )

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


# ---------------------------------------------------------------------------
# SqlaTable
# ---------------------------------------------------------------------------


class SqlaTable(Base, AuditMixinNullable, ImportExportMixin, BaseDatasource):
    """A SQL dataset (table or virtual query)."""

    __tablename__ = "tables"
    __table_args__ = (
        UniqueConstraint(
            "database_id", "catalog", "schema", "table_name"
        ),
    )

    id = Column(Integer, primary_key=True)
    table_name = Column(String(250))
    main_dttm_col = Column(String(250))
    database_id = Column(
        Integer, ForeignKey("dbs.id"), nullable=False
    )
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
