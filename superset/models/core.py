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
"""Core models: Database, Log, FavStar, CssTemplate, Theme, KeyValue.

Pure SQLAlchemy -- no Flask dependencies.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    ImportExportMixin,
    MediumText,
    UUIDMixin,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ConfigurationMethod(str, enum.Enum):
    """How a Database connection was configured."""

    SQLALCHEMY_FORM = "sqlalchemy_form"
    DYNAMIC_FORM = "dynamic_form"


class FavStarClassName(str, enum.Enum):
    """Entity types that can be favorited."""

    CHART = "slice"
    DASHBOARD = "Dashboard"
    DATASET = "SqlaTable"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class KeyValue(Base):
    """Legacy generic key-value store (table ``keyvalue``).

    This is the original Superset key-value table used for simple text
    storage (e.g., filter state, permalink data in older versions).
    It stores values as :class:`MediumText` and has no audit columns.

    Not to be confused with :class:`superset.models.key_value.KeyValueEntry`
    which maps to the newer ``key_value`` table and supports binary values,
    resource namespacing, expiration, and full audit tracking.
    """

    __tablename__ = "keyvalue"

    id = Column(Integer, primary_key=True)
    value = Column(MediumText(), nullable=False)


class CssTemplate(AuditMixinNullable, UUIDMixin, Base):
    """Custom CSS templates for dashboards."""

    __tablename__ = "css_templates"

    id = Column(Integer, primary_key=True)
    template_name = Column(String(250))
    css = Column(MediumText(), default="")


class Theme(AuditMixinNullable, ImportExportMixin, Base):
    """Dashboard theme definitions."""

    __tablename__ = "themes"
    __table_args__ = (
        Index("idx_theme_is_system_default", "is_system_default"),
        Index("idx_theme_is_system_dark", "is_system_dark"),
    )

    id = Column(Integer, primary_key=True)
    theme_name = Column(String(250))
    json_data = Column(MediumText(), default="")
    is_system = Column(Boolean, default=False, nullable=False)
    is_system_default = Column(Boolean, default=False, nullable=False)
    is_system_dark = Column(Boolean, default=False, nullable=False)


class Database(AuditMixinNullable, ImportExportMixin, Base):
    """A database connection registered in Superset."""

    __tablename__ = "dbs"
    __table_args__ = (UniqueConstraint("database_name"),)

    id = Column(Integer, primary_key=True)
    verbose_name = Column(String(250), unique=True)
    database_name = Column(String(250), unique=True, nullable=False)
    sqlalchemy_uri = Column(String(1024), nullable=False)
    password = Column(Text)
    cache_timeout = Column(Integer)
    select_as_create_table_as = Column(Boolean, default=False)
    expose_in_sqllab = Column(Boolean, default=True)
    configuration_method = Column(
        String(255),
        server_default=ConfigurationMethod.SQLALCHEMY_FORM.value,
    )
    allow_run_async = Column(Boolean, default=False)
    allow_file_upload = Column(Boolean, default=False)
    allow_ctas = Column(Boolean, default=False)
    allow_cvas = Column(Boolean, default=False)
    allow_dml = Column(Boolean, default=False)
    force_ctas_schema = Column(String(250))
    extra = Column(Text, default="{}")
    encrypted_extra = Column(Text, nullable=True)
    impersonate_user = Column(Boolean, default=False)
    server_cert = Column(Text, nullable=True)
    is_managed_externally = Column(
        Boolean, nullable=False, default=False
    )
    external_url = Column(Text, nullable=True)


class DatabaseUserOAuth2Tokens(AuditMixinNullable, Base):
    """OAuth2 tokens for per-user database authentication."""

    __tablename__ = "database_user_oauth2_tokens"
    __table_args__ = (
        Index("idx_user_id_database_id", "user_id", "database_id"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("ab_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    database_id = Column(
        Integer,
        ForeignKey("dbs.id", ondelete="CASCADE"),
        nullable=False,
    )
    access_token = Column(Text, nullable=True)
    access_token_expiration = Column(
        DateTime, nullable=True
    )
    refresh_token = Column(Text, nullable=True)

    user = relationship(
        "User",
        foreign_keys=[user_id],
    )
    database = relationship(
        "Database",
        foreign_keys=[database_id],
    )


class Log(Base):
    """Action audit log."""

    __tablename__ = "logs"

    id = Column(Integer, primary_key=True)
    action = Column(String(512))
    user_id = Column(Integer, ForeignKey("ab_user.id"))
    dashboard_id = Column(Integer)
    slice_id = Column(Integer)
    json = Column(MediumText())
    dttm = Column(DateTime, default=datetime.utcnow)
    duration_ms = Column(Integer)
    referrer = Column(String(1024))

    user = relationship(
        "User",
        foreign_keys=[user_id],
    )


class FavStar(UUIDMixin, Base):
    """Favorite stars for charts, dashboards, and datasets."""

    __tablename__ = "favstar"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("ab_user.id"))
    class_name = Column(String(50))
    obj_id = Column(Integer)
    dttm = Column(DateTime, default=datetime.utcnow)
