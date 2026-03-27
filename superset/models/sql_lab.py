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
"""SQL Lab models: Query, SavedQuery, TabState, TableSchema.

Pure SQLAlchemy -- no Flask dependencies.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    ExtraJSONMixin,
    ImportExportMixin,
    LongText,
    MediumText,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LimitingFactor(str, enum.Enum):
    """What limited the number of rows returned by a query."""

    UNKNOWN = "UNKNOWN"
    LIMITED = "LIMITED"
    QUERY = "QUERY"
    QUERY_AND_DROPDOWN = "QUERY_AND_DROPDOWN"
    NOT_LIMITED = "NOT_LIMITED"
    DROPDOWN = "DROPDOWN"


class CTASMethod(str, enum.Enum):
    """How a CREATE TABLE AS / CREATE VIEW AS is executed."""

    TABLE = "TABLE"
    VIEW = "VIEW"


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class Query(Base, ExtraJSONMixin):
    """A SQL query executed in SQL Lab."""

    __tablename__ = "query"
    __table_args__ = (
        Index("ti_user_id_changed_on", "user_id", "changed_on"),
    )

    id = Column(Integer, primary_key=True)
    client_id = Column(String(11), unique=True, nullable=False)
    database_id = Column(
        Integer, ForeignKey("dbs.id"), nullable=False
    )
    tmp_table_name = Column(String(256))
    tmp_schema_name = Column(String(256))
    user_id = Column(
        Integer, ForeignKey("ab_user.id"), nullable=True
    )
    status = Column(String(16), default="pending")
    tab_name = Column(String(256))
    sql_editor_id = Column(String(256), index=True)
    schema = Column(String(256))
    catalog = Column(String(256), nullable=True, default=None)
    sql = Column(LongText())
    select_sql = Column(LongText())
    executed_sql = Column(LongText())
    limit = Column(Integer)
    limiting_factor = Column(
        Enum(LimitingFactor), server_default="UNKNOWN"
    )
    select_as_cta = Column(Boolean)
    select_as_cta_used = Column(Boolean, default=False)
    ctas_method = Column(String(16), default="TABLE")
    progress = Column(Integer, default=0)
    rows = Column(Integer)
    error_message = Column(Text)
    results_key = Column(String(64), index=True)
    start_time = Column(Numeric(precision=20, scale=6))
    start_running_time = Column(
        Numeric(precision=20, scale=6)
    )
    end_time = Column(Numeric(precision=20, scale=6))
    end_result_backend_time = Column(
        Numeric(precision=20, scale=6)
    )
    tracking_url_raw = Column(Text, name="tracking_url")
    changed_on = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=True,
    )

    # -- relationships --------------------------------------------------------

    database = relationship(
        "Database",
        foreign_keys=[database_id],
    )
    user = relationship(
        "User",
        foreign_keys=[user_id],
    )


# ---------------------------------------------------------------------------
# SavedQuery
# ---------------------------------------------------------------------------


class SavedQuery(Base, AuditMixinNullable, ExtraJSONMixin, ImportExportMixin):
    """A saved SQL query."""

    __tablename__ = "saved_query"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("ab_user.id"), nullable=True
    )
    db_id = Column(
        Integer, ForeignKey("dbs.id"), nullable=True
    )
    schema = Column(String(128))
    catalog = Column(String(256), nullable=True, default=None)
    label = Column(String(256))
    description = Column(Text)
    sql = Column(MediumText())
    template_parameters = Column(Text)
    rows = Column(Integer)
    last_run = Column(DateTime)

    # -- relationships --------------------------------------------------------

    user = relationship(
        "User",
        foreign_keys=[user_id],
    )
    database = relationship(
        "Database",
        foreign_keys=[db_id],
    )


# ---------------------------------------------------------------------------
# TabState
# ---------------------------------------------------------------------------


class TabState(AuditMixinNullable, ExtraJSONMixin, Base):
    """Persisted SQL Lab tab state."""

    __tablename__ = "tab_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("ab_user.id"), nullable=True
    )
    label = Column(String(256))
    active = Column(Boolean, default=False)
    database_id = Column(
        Integer,
        ForeignKey("dbs.id", ondelete="CASCADE"),
        nullable=True,
    )
    schema = Column(String(256))
    catalog = Column(String(256), nullable=True, default=None)
    sql = Column(MediumText())
    query_limit = Column(Integer)
    latest_query_id = Column(
        String(11),
        ForeignKey("query.client_id", ondelete="SET NULL"),
        nullable=True,
    )
    autorun = Column(Boolean, default=False)
    template_params = Column(Text)
    hide_left_bar = Column(Boolean, default=False)
    saved_query_id = Column(
        Integer,
        ForeignKey("saved_query.id", ondelete="SET NULL"),
        nullable=True,
    )

    # -- relationships --------------------------------------------------------

    database = relationship(
        "Database",
        foreign_keys=[database_id],
    )
    table_schemas = relationship(
        "TableSchema",
        cascade="all, delete-orphan",
        backref="tab_state",
    )
    latest_query = relationship(
        "Query",
        foreign_keys=[latest_query_id],
    )
    saved_query = relationship(
        "SavedQuery",
        foreign_keys=[saved_query_id],
    )


# ---------------------------------------------------------------------------
# TableSchema
# ---------------------------------------------------------------------------


class TableSchema(AuditMixinNullable, ExtraJSONMixin, Base):
    """Schema metadata for a table displayed in SQL Lab's left panel."""

    __tablename__ = "table_schema"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tab_state_id = Column(
        Integer,
        ForeignKey("tab_state.id", ondelete="CASCADE"),
        nullable=True,
    )
    database_id = Column(
        Integer,
        ForeignKey("dbs.id", ondelete="CASCADE"),
        nullable=True,
    )
    schema = Column(String(256))
    catalog = Column(String(256), nullable=True, default=None)
    table = Column(String(256))
    description = Column(Text)
    expanded = Column(Boolean, default=False)

    # -- relationships --------------------------------------------------------

    database = relationship(
        "Database",
        foreign_keys=[database_id],
    )
