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
"""Core models: Database, Log, FavStar, CssTemplate, Theme, KeyValue.

Pure SQLAlchemy -- no Flask dependencies.
"""

from __future__ import annotations

import enum
import json
import logging
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from typing import Any

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
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import NoSuchModuleError
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship
from sqlalchemy.sql import expression

from superset.constants import LRU_CACHE_MAX_SIZE, PASSWORD_MASK
from superset.databases.utils import DatabaseInvalidError, make_url_safe
from superset.db_engine_specs import BaseEngineSpec, get_engine_spec
from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    ImportExportMixin,
    MediumText,
    UUIDMixin,
)

logger = logging.getLogger(__name__)

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
    is_managed_externally = Column(Boolean, nullable=False, default=False)
    external_url = Column(Text, nullable=True)

    export_fields = [
        "database_name",
        "sqlalchemy_uri",
        "cache_timeout",
        "expose_in_sqllab",
        "allow_run_async",
        "allow_ctas",
        "allow_cvas",
        "allow_dml",
        "allow_file_upload",
        "extra",
        "impersonate_user",
    ]
    extra_import_fields = [
        "password",
        "is_managed_externally",
        "external_url",
        "encrypted_extra",
        "impersonate_user",
    ]

    def __repr__(self) -> str:
        return self.name

    # ------------------------------------------------------------------
    # Core properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.verbose_name if self.verbose_name else self.database_name

    @property
    def unique_name(self) -> str:
        return self.database_name

    @property
    def sqlalchemy_uri_decrypted(self) -> str:
        """Full URI with password unmasked."""
        try:
            conn = make_url_safe(self.sqlalchemy_uri)
        except DatabaseInvalidError:
            # if the URI is invalid, ignore and return a placeholder url
            # (so users see 500 less often)
            return "dialect://invalid_uri"
        conn = conn.set(password=self.password)
        return str(conn)

    @property
    def url_object(self) -> URL:
        return make_url_safe(self.sqlalchemy_uri_decrypted)

    @property
    def backend(self) -> str:
        return self.url_object.get_backend_name()

    @property
    def driver(self) -> str:
        return self.url_object.get_driver_name()

    # ------------------------------------------------------------------
    # Engine spec
    # ------------------------------------------------------------------

    @property
    def db_engine_spec(self) -> type[BaseEngineSpec]:
        url = make_url_safe(self.sqlalchemy_uri_decrypted)
        return self.get_db_engine_spec(url)

    @classmethod
    @lru_cache(maxsize=LRU_CACHE_MAX_SIZE)
    def get_db_engine_spec(cls, url: URL) -> type[BaseEngineSpec]:
        backend = url.get_backend_name()
        try:
            driver = url.get_driver_name()
        except NoSuchModuleError:
            # can't load the driver, fallback for backwards compatibility
            driver = None

        return get_engine_spec(backend, driver)

    # ------------------------------------------------------------------
    # Extra / encrypted_extra helpers
    # ------------------------------------------------------------------

    def get_extra(self) -> dict[str, Any]:
        """Parse the JSON ``extra`` column into a dict."""
        extra: dict[str, Any] = {}
        if self.extra:
            try:
                extra = json.loads(self.extra)
            except json.JSONDecodeError as ex:
                logger.error(ex, exc_info=True)
                raise
        return extra

    def get_encrypted_extra(self) -> dict[str, Any]:
        encrypted_extra: dict[str, Any] = {}
        if self.encrypted_extra:
            try:
                encrypted_extra = json.loads(self.encrypted_extra)
            except json.JSONDecodeError as ex:
                logger.error(ex, exc_info=True)
                raise
        return encrypted_extra

    @property
    def masked_encrypted_extra(self) -> str | None:
        if hasattr(self.db_engine_spec, "mask_encrypted_extra"):
            return self.db_engine_spec.mask_encrypted_extra(self.encrypted_extra)
        return self.encrypted_extra

    # ------------------------------------------------------------------
    # Capability flags (derived from extra / db_engine_spec)
    # ------------------------------------------------------------------

    @property
    def allows_subquery(self) -> bool:
        return getattr(self.db_engine_spec, "allows_subqueries", True)

    @property
    def allows_cost_estimate(self) -> bool:
        extra = self.get_extra() or {}
        cost_estimate_enabled: bool = extra.get("cost_estimate_enabled")  # type: ignore[assignment]

        if hasattr(self.db_engine_spec, "get_allow_cost_estimate"):
            return (
                self.db_engine_spec.get_allow_cost_estimate(extra)
                and cost_estimate_enabled
            )
        return bool(cost_estimate_enabled)

    @property
    def allows_virtual_table_explore(self) -> bool:
        extra = self.get_extra()
        return bool(extra.get("allows_virtual_table_explore", True))

    @property
    def explore_database_id(self) -> int:
        return self.get_extra().get("explore_database_id", self.id)

    @property
    def disable_data_preview(self) -> bool:
        # this will prevent any 'trash value' strings from going through
        return self.get_extra().get("disable_data_preview", False) is True

    @property
    def disable_drill_to_detail(self) -> bool:
        # this will prevent any 'trash value' strings from going through
        return self.get_extra().get("disable_drill_to_detail", False) is True

    @property
    def allow_multi_catalog(self) -> bool:
        return self.get_extra().get("allow_multi_catalog", False)

    @property
    def schema_options(self) -> dict[str, Any]:
        """Additional schema display config for engines with complex schemas."""
        return self.get_extra().get("schema_options", {})

    # ------------------------------------------------------------------
    # Cache-related properties
    # ------------------------------------------------------------------

    @property
    def metadata_cache_timeout(self) -> dict[str, Any]:
        return self.get_extra().get("metadata_cache_timeout", {})

    @property
    def catalog_cache_enabled(self) -> bool:
        return "catalog_cache_timeout" in self.metadata_cache_timeout

    @property
    def catalog_cache_timeout(self) -> int | None:
        return self.metadata_cache_timeout.get("catalog_cache_timeout")

    @property
    def schema_cache_enabled(self) -> bool:
        return "schema_cache_timeout" in self.metadata_cache_timeout

    @property
    def schema_cache_timeout(self) -> int | None:
        return self.metadata_cache_timeout.get("schema_cache_timeout")

    @property
    def table_cache_enabled(self) -> bool:
        return "table_cache_timeout" in self.metadata_cache_timeout

    @property
    def table_cache_timeout(self) -> int | None:
        return self.metadata_cache_timeout.get("table_cache_timeout")

    @property
    def default_schemas(self) -> list[str]:
        return self.get_extra().get("default_schemas", [])

    @property
    def connect_args(self) -> dict[str, Any]:
        return self.get_extra().get("engine_params", {}).get("connect_args", {})

    # ------------------------------------------------------------------
    # Parameters / engine information
    # ------------------------------------------------------------------

    @property
    def parameters(self) -> dict[str, Any]:
        """Database parameters derived from the masked SQLAlchemy URI.

        When returning the parameters we use the masked SQLAlchemy URI and the
        masked ``encrypted_extra`` to prevent exposing sensitive credentials.
        """
        masked_uri = make_url_safe(self.sqlalchemy_uri)
        encrypted_config: dict[str, Any] = {}
        if (masked_encrypted_extra := self.masked_encrypted_extra) is not None:
            try:
                encrypted_config = json.loads(masked_encrypted_extra)
            except (TypeError, json.JSONDecodeError):
                pass
        try:
            if hasattr(self.db_engine_spec, "get_parameters_from_uri"):
                parameters = self.db_engine_spec.get_parameters_from_uri(  # type: ignore[attr-defined]
                    masked_uri,
                    encrypted_extra=encrypted_config,
                )
            else:
                parameters = {}
        except Exception:  # noqa: BLE001
            parameters = {}

        return parameters

    @property
    def parameters_schema(self) -> dict[str, Any]:
        try:
            if hasattr(self.db_engine_spec, "parameters_json_schema"):
                parameters_schema = self.db_engine_spec.parameters_json_schema()  # type: ignore[attr-defined]
            else:
                parameters_schema = {}
        except Exception:  # noqa: BLE001
            parameters_schema = {}
        return parameters_schema

    @property
    def engine_information(self) -> dict[str, Any]:
        try:
            if hasattr(self.db_engine_spec, "get_public_information"):
                engine_information = self.db_engine_spec.get_public_information()  # type: ignore[attr-defined]
            else:
                engine_information = {}
        except Exception:  # noqa: BLE001
            engine_information = {}
        return engine_information

    @property
    def function_names(self) -> list[str]:
        try:
            if hasattr(self.db_engine_spec, "get_function_names"):
                return self.db_engine_spec.get_function_names(self)  # type: ignore[attr-defined]
        except Exception as ex:  # noqa: BLE001
            # function_names property is used in bulk APIs and should not hard crash
            # more info in: https://github.com/apache/superset/issues/9678
            logger.error(
                "Failed to fetch database function names with error: %s",
                str(ex),
                exc_info=True,
            )
        return []

    # ------------------------------------------------------------------
    # data — dict serialization for API responses
    # ------------------------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.database_name,
            "backend": self.backend,
            "configuration_method": self.configuration_method,
            "allows_subquery": self.allows_subquery,
            "allows_cost_estimate": self.allows_cost_estimate,
            "allows_virtual_table_explore": self.allows_virtual_table_explore,
            "explore_database_id": self.explore_database_id,
            "schema_options": self.schema_options,
            "parameters": self.parameters,
            "disable_data_preview": self.disable_data_preview,
            "disable_drill_to_detail": self.disable_drill_to_detail,
            "allow_multi_catalog": self.allow_multi_catalog,
            "parameters_schema": self.parameters_schema,
            "engine_information": self.engine_information,
        }

    # ------------------------------------------------------------------
    # Perm
    # ------------------------------------------------------------------

    @hybrid_property
    def perm(self) -> str:
        return f"[{self.database_name}].(id:{self.id})"

    @perm.expression  # type: ignore[no-redef]
    def perm(cls) -> str:  # noqa: N805
        return (
            "[" + cls.database_name + "].(id:" + expression.cast(cls.id, String) + ")"
        )

    def get_perm(self) -> str:
        return self.perm  # type: ignore[return-value]

    @property
    def sql_url(self) -> str:
        return f"/superset/sql/{self.id}/"

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    @classmethod
    def get_password_masked_url_from_uri(cls, uri: str) -> URL:
        sqlalchemy_url = make_url_safe(uri)
        return cls.get_password_masked_url(sqlalchemy_url)

    @classmethod
    def get_password_masked_url(cls, masked_url: URL) -> URL:
        url_copy = deepcopy(masked_url)
        if url_copy.password is not None:
            url_copy = url_copy.set(password=PASSWORD_MASK)
        return url_copy

    def set_sqlalchemy_uri(self, uri: str) -> None:
        conn = make_url_safe(uri.strip())
        if conn.password != PASSWORD_MASK:
            # do not over-write the password with the password mask
            self.password = conn.password
        conn = conn.set(password=PASSWORD_MASK if conn.password else None)
        self.sqlalchemy_uri = str(conn)  # hides the password

    def safe_sqlalchemy_uri(self) -> str:
        return self.sqlalchemy_uri

    def get_effective_user(self, object_url: URL) -> str | None:
        """Get the effective user, especially during impersonation.

        :param object_url: SQL Alchemy URL object
        :return: The effective username
        """
        return object_url.username if self.impersonate_user else None


class DatabaseUserOAuth2Tokens(AuditMixinNullable, Base):
    """OAuth2 tokens for per-user database authentication."""

    __tablename__ = "database_user_oauth2_tokens"
    __table_args__ = (Index("idx_user_id_database_id", "user_id", "database_id"),)

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
    access_token_expiration = Column(DateTime, nullable=True)
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
