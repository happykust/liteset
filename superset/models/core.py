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
import textwrap
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
from superset.db_engine_specs.base import TimeGrain
from superset.extensions import encrypted_field_factory
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

    # Mirrors original Superset Theme.export_fields at
    # superset_old/models/core.py:134
    export_fields = ["theme_name", "json_data"]


class Database(AuditMixinNullable, ImportExportMixin, Base):
    """A database connection registered in Superset."""

    __tablename__ = "dbs"
    __table_args__ = (UniqueConstraint("database_name"),)

    id = Column(Integer, primary_key=True)
    verbose_name = Column(String(250), unique=True)
    database_name = Column(String(250), unique=True, nullable=False)
    sqlalchemy_uri = Column(String(1024), nullable=False)
    password = Column(encrypted_field_factory.create(String(1024)))
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
    # 1:1 with superset_old/models/core.py: Database.extra defaults to the
    # full JSON template (not "{}"), so new databases expose the expected
    # metadata_params/engine_params/metadata_cache_timeout/schemas_allowed keys.
    extra = Column(
        Text,
        default=textwrap.dedent(
            """\
    {
        "metadata_params": {},
        "engine_params": {},
        "metadata_cache_timeout": {},
        "schemas_allowed_for_file_upload": []
    }
    """
        ),
    )
    encrypted_extra = Column(encrypted_field_factory.create(Text), nullable=True)
    impersonate_user = Column(Boolean, default=False)
    server_cert = Column(encrypted_field_factory.create(Text), nullable=True)
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
        """Full URI with password unmasked.

        Used by every code path that opens a connection to the user
        database (``get_async_connection``, ``get_sqla_engine`` …).
        SA 2.0's ``str(URL)`` masks the password as ``***`` regardless
        of the value set on the URL object — we have to use
        ``render_as_string(hide_password=False)`` to actually emit the
        plaintext password from the encrypted ``password`` column.
        """
        try:
            conn = make_url_safe(self.sqlalchemy_uri)
        except DatabaseInvalidError:
            # if the URI is invalid, ignore and return a placeholder url
            # (so users see 500 less often)
            return "dialect://invalid_uri"
        conn = conn.set(password=self.password)
        return conn.render_as_string(hide_password=False)

    @property
    def url_object(self) -> URL:
        return make_url_safe(self.sqlalchemy_uri_decrypted)

    @property
    def backend(self) -> str:
        return self.url_object.get_backend_name()

    @property
    def driver(self) -> str:
        return self.url_object.get_driver_name()

    def get_dialect(self) -> Any:
        """Return an instantiated SQLAlchemy dialect for this database.

        Ported 1:1 from the original sync ``Database.get_dialect``
        (``superset_old/models/core.py:1125``) — used by the Jinja
        template processor, ``WhereInMacro``, and other code paths
        that need dialect-specific behavior (identifier quoting,
        reserved words, literal binding).
        """
        sqla_url = make_url_safe(self.sqlalchemy_uri_decrypted)
        return sqla_url.get_dialect()()

    def quote_identifier(self, name: str) -> str:
        """Conditionally quote an identifier using the dialect's preparer.

        1:1 with ``Database.quote_identifier`` in
        ``superset_old/models/core.py``
        (line 645) — used by ``adhoc_column_to_sqla`` and
        ``Database.compile_sqla_query`` to safely render bare column /
        catalog / schema references.
        """
        return self.get_dialect().identifier_preparer.quote(name)

    def get_reserved_words(self) -> set[str]:
        """1:1 with original (line 649)."""
        return self.get_dialect().preparer.reserved_words

    def get_default_catalog(self) -> str | None:
        """Return the default catalog for this database.

        1:1 with ``get_default_catalog`` in
        ``superset_old/db_engine_specs/base.py``
        (line 678) — most engines don't support catalogs at all and
        return ``None``; the engine spec overrides this for engines
        that do (BigQuery → project, Trino → catalog).
        """
        spec = self.db_engine_spec
        if spec is not None and hasattr(spec, "get_default_catalog"):
            try:
                return spec.get_default_catalog(self)
            except Exception:  # noqa: BLE001
                return None
        return None

    def get_default_schema(self, catalog: str | None = None) -> str | None:
        """Return the default schema for this database.

        1:1 with ``Database.get_default_schema`` in
        ``superset_old/models/core.py``
        (line 604) — delegates to the engine spec's
        :meth:`get_default_schema`, which lets dialect-specific
        overrides (Postgres → ``public``, BigQuery → project-default,
        etc.) take effect rather than the bare SQLAlchemy
        ``inspector.default_schema_name`` fallback.
        """
        spec = self.db_engine_spec
        if spec is None:
            return None
        try:
            return spec.get_default_schema(self, catalog)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Could not introspect default schema for database %s",
                self.database_name,
                exc_info=True,
            )
            return None

    def get_default_schema_for_query(
        self,
        query: Any,
        template_params: dict[str, Any] | None = None,
    ) -> str | None:
        """Return the default schema for a given query.

        1:1 with ``Database.get_default_schema_for_query`` in
        ``superset_old/models/core.py``
        — delegates to the engine spec so dialects that compute the
        default schema dynamically (e.g. ``USE schema``-aware engines)
        can override.
        """
        spec = self.db_engine_spec
        if spec is None:
            return None
        try:
            return spec.get_default_schema_for_query(self, query, template_params)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Could not resolve query default schema for database %s",
                self.database_name,
                exc_info=True,
            )
            return None

    def get_sqla_engine(
        self,
        catalog: str | None = None,
        schema: str | None = None,
        source: Any | None = None,
        nullpool: bool = True,
        override_ssh_tunnel: Any | None = None,
    ) -> Any:
        """Return a context manager yielding a sync SQLAlchemy engine.

        1:1 with ``Database.get_sqla_engine`` in
        ``superset_old/models/core.py``.
        Wraps ``superset.utils.database.get_sync_engine`` which is the
        Liteset-side equivalent of the Flask app's sync engine
        registry. Used by ``compile_sqla_query`` and ``get_df``.

        When ``override_ssh_tunnel`` is supplied, the engine is created
        through the SSH tunnel (opened/torn down by the context manager),
        mirroring upstream's ``override_ssh_tunnel`` parameter.
        """
        from superset.utils.database import get_sync_engine

        return get_sync_engine(
            self,
            catalog=catalog,
            schema=schema,
            nullpool=nullpool,
            override_ssh_tunnel=override_ssh_tunnel,
        )

    def get_inspector(
        self,
        catalog: str | None = None,
        schema: str | None = None,
    ) -> Any:
        """Return a context manager yielding a SQLAlchemy Inspector.

        1:1 with ``Database.get_inspector`` in
        ``superset_old/models/core.py``
        (line 875). Both ``catalog`` and ``schema`` are forwarded to
        ``get_sqla_engine`` so dialects that scope inspectors per
        catalog/schema (e.g. BigQuery / MSSQL) bind to the right
        namespace.
        """
        from contextlib import contextmanager

        from sqlalchemy import inspect as sa_inspect

        @contextmanager
        def _ctx() -> Any:
            from superset.utils.database import get_sync_engine

            with get_sync_engine(self, catalog=catalog, schema=schema) as engine:
                yield sa_inspect(engine)

        return _ctx()

    def get_metrics(self, table: Any) -> list[dict[str, Any]]:
        """Fetch metric definitions for a table via the engine spec.

        1:1 with ``Database.get_metrics`` in
        ``superset_old/models/core.py`` (line 1016). ``table`` is a
        :class:`superset.sql.parse.Table`. Synchronous (inspector-backed),
        so callers in the async runtime must run it in a thread.
        """
        with self.get_inspector(
            catalog=table.catalog,
            schema=table.schema,
        ) as inspector:
            return self.db_engine_spec.get_metrics(self, inspector, table)

    async def get_all_table_names_in_schema(
        self,
        *,
        catalog: str | None,
        schema: str,
        force: bool = False,
        cache: bool = False,
        cache_timeout: int | None = None,
    ) -> set[tuple[str, str, str | None]]:
        """1:1 with ``Database.get_all_table_names_in_schema`` in
        ``superset_old/models/core.py``.

        The original is sync and decorated with ``cache_util.memoized_func``
        for Flask-Caching.  The async port wraps the sync inspector call
        with :func:`superset.utils.cache.memoized_func` so the same
        cache-key shape is reused (``db:{id}:catalog:{c}:schema:{s}:table_list``).
        ``force`` / ``cache`` / ``cache_timeout`` are honoured by the
        decorator at call-time exactly as in the original.
        """

        from superset.extensions import cache_manager
        from superset.utils.cache import memoized_func

        @memoized_func(  # type: ignore[misc]
            key="db:{database_id}:catalog:{catalog}:schema:{schema}:table_list",
            cache=cache_manager.cache,
        )
        async def _impl(
            database_id: int,
            catalog: str | None,
            schema: str,
        ) -> set[tuple[str, str, str | None]]:
            try:
                with self.get_inspector(catalog=catalog, schema=schema) as inspector:
                    return {
                        (table, schema, catalog)
                        for table in self.db_engine_spec.get_table_names(
                            database=self,
                            inspector=inspector,
                            schema=schema,
                        )
                    }
            except Exception as ex:  # noqa: BLE001
                raise self.db_engine_spec.get_dbapi_mapped_exception(ex) from ex

        return await _impl(  # type: ignore[no-any-return]
            self.id,
            catalog,
            schema,
            force=force,
            cache=cache,
            cache_timeout=cache_timeout,
        )

    async def get_all_view_names_in_schema(
        self,
        *,
        catalog: str | None,
        schema: str,
        force: bool = False,
        cache: bool = False,
        cache_timeout: int | None = None,
    ) -> set[tuple[str, str, str | None]]:
        """1:1 with ``Database.get_all_view_names_in_schema`` in
        ``superset_old/models/core.py``."""

        from superset.extensions import cache_manager
        from superset.utils.cache import memoized_func

        @memoized_func(  # type: ignore[misc]
            key="db:{database_id}:catalog:{catalog}:schema:{schema}:view_list",
            cache=cache_manager.cache,
        )
        async def _impl(
            database_id: int,
            catalog: str | None,
            schema: str,
        ) -> set[tuple[str, str, str | None]]:
            try:
                with self.get_inspector(catalog=catalog, schema=schema) as inspector:
                    return {
                        (view, schema, catalog)
                        for view in self.db_engine_spec.get_view_names(
                            database=self,
                            inspector=inspector,
                            schema=schema,
                        )
                    }
            except Exception as ex:  # noqa: BLE001
                raise self.db_engine_spec.get_dbapi_mapped_exception(ex) from ex

        return await _impl(  # type: ignore[no-any-return]
            self.id,
            catalog,
            schema,
            force=force,
            cache=cache,
            cache_timeout=cache_timeout,
        )

    def make_sqla_column_compatible(
        self, sqla_col: Any, label: str | None = None
    ) -> Any:
        """Take care of metric formatting / aliasing.

        1:1 with ``Database.make_sqla_column_compatible`` in
        ``superset_old/models/core.py``
        (line 1129). Honours the engine spec's
        ``get_allows_alias_in_select`` and ``make_label_compatible`` —
        crucial for Oracle's 30-char label truncation and MSSQL's
        bracketed alias quoting.
        """
        label_expected = label or sqla_col.name
        if self.db_engine_spec.get_allows_alias_in_select(self):
            label = self.db_engine_spec.make_label_compatible(label_expected)
            sqla_col = sqla_col.label(label)
        sqla_col.key = label_expected
        return sqla_col

    def mutate_sql_based_on_config(self, sql_: str, is_split: bool = False) -> str:
        """Apply ``SQL_QUERY_MUTATOR`` config hook to the SQL.

        1:1 with ``Database.mutate_sql_based_on_config`` in
        ``superset_old/models/core.py``
        (line 652). The mutator is loaded from ``superset.config`` —
        ``superset_config.py`` users can register a function as
        ``SQL_QUERY_MUTATOR``. Honours ``MUTATE_AFTER_SPLIT`` so the
        mutator either runs once on the whole script or on each
        individual statement post-split.

        ``security_manager`` is passed as a sync read-only proxy
        (:class:`superset.security.manager.SyncSecurityManagerProxy`)
        which mirrors the read-only API surface mutators commonly
        need — ``get_user_id`` / ``current_user`` / ``is_user_admin``
        — while keeping the async :class:`AsyncSecurityManager`
        separate.  Mutators that relied on the original
        FAB ``SecurityManager`` for those three methods continue to
        work unchanged.
        """
        try:
            from superset import config as _config
        except ImportError:
            return sql_

        # Two configuration discovery paths:
        # 1. Legacy uppercase module-level constants
        #    (``SQL_QUERY_MUTATOR`` / ``MUTATE_AFTER_SPLIT``) — what
        #    superset_config.py users have always set.
        # 2. Pydantic settings (``sql_query_mutator`` /
        #    ``mutate_after_split``) — Liteset's preferred form.
        sql_mutator = getattr(_config, "SQL_QUERY_MUTATOR", None)
        mutate_after_split = getattr(_config, "MUTATE_AFTER_SPLIT", False)
        if sql_mutator is None:
            try:
                settings = _config.SupersetSettings()
                sql_mutator = getattr(settings, "sql_query_mutator", None)
                mutate_after_split = getattr(settings, "mutate_after_split", False)
            except Exception:  # noqa: BLE001, S110
                pass

        if sql_mutator and (is_split == mutate_after_split):
            from superset.security.manager import (
                get_sync_security_manager_proxy,
            )

            sm_proxy = get_sync_security_manager_proxy()
            # 1:1 with superset_old/models/core.py:663-669 — only
            # ``security_manager`` and ``database`` are passed; the
            # current user is reachable via ``sm_proxy.current_user``.
            return sql_mutator(
                sql_,
                security_manager=sm_proxy,
                database=self,
            )
        return sql_

    def compile_sqla_query(
        self,
        qry: Any,
        catalog: str | None = None,
        schema: str | None = None,
        is_virtual: bool = False,
    ) -> str:
        """Compile a SQLAlchemy ``Select`` AST to a SQL string.

        1:1 with ``Database.compile_sqla_query`` in
        ``superset_old/models/core.py``
        (line 741). Uses the dialect's identifier preparer for
        engine-correct quoting and handles the ``%%`` double-percent
        fixup that some DB-API drivers require. When ``is_virtual=True``
        and the ``OPTIMIZE_SQL`` feature flag is enabled the rendered
        SQL is also routed through :class:`SQLScript.optimize` which
        applies predicate-pushdown and other SQLGlot-driven
        optimizations on the virtual-dataset SELECT.
        """
        # Match the original by reading the dialect from the engine
        # bound to ``catalog`` / ``schema`` so dialect plug-ins that
        # vary per-catalog (BigQuery / Trino) get the right preparer.
        with self.get_sqla_engine(catalog=catalog, schema=schema) as engine:
            sql = str(qry.compile(engine, compile_kwargs={"literal_binds": True}))
            # pylint: disable=protected-access
            if getattr(engine.dialect.identifier_preparer, "_double_percents", False):
                sql = sql.replace("%%", "%")

        # OPTIMIZE_SQL — only meaningful for virtual datasources where
        # SQLGlot can push predicates / prune projections through the
        # outer SELECT. 1:1 with original (line 757-759). Failures
        # bubble up, matching the original's behaviour.
        if is_virtual and self._is_optimize_sql_enabled():
            from superset.sql.parse import SQLScript

            script = SQLScript(sql, engine=self.db_engine_spec.engine).optimize()
            sql = script.format()

        return sql

    @staticmethod
    def _is_optimize_sql_enabled() -> bool:
        """Return whether the ``OPTIMIZE_SQL`` feature flag is enabled.

        Matches the original's ``is_feature_enabled('OPTIMIZE_SQL')``
        check. Wrapped in a static method so callers don't have to
        import :mod:`superset.utils.feature_flags` directly.
        """
        from superset.utils.feature_flags import feature_flag_manager

        return feature_flag_manager.is_feature_enabled("OPTIMIZE_SQL")

    def get_df(
        self,
        sql: str,
        catalog: str | None = None,
        schema: str | None = None,
        mutator: Any | None = None,
    ) -> Any:
        """Execute SQL and return a pandas DataFrame.

        Sync variant of the chart-data execute path. 1:1 with
        ``superset_old/models/core.py:Database.get_df`` (line 672) —
        runs the script statement-by-statement, applies the mutator on
        the final result, and returns a DataFrame.

        Used by helpers.ExploreMixin.exc_query and the prequery
        pipeline (series-limit fallback for engines without subquery
        joins). Most production paths in Liteset are async — this
        method exists so the synchronous helpers code path remains
        runnable when invoked from a worker thread.

        1:1 with ``superset_old/models/core.py:Database.get_df``: runs the
        script statement-by-statement through the **engine spec**
        (``execute`` + ``fetch_data``) and materialises rows via
        :class:`SupersetResultSet`, so column de-duplication / type
        coercion match every other code path (the previous implementation
        bypassed the engine spec and built the DataFrame from raw rows).
        """
        from contextlib import closing

        import pandas as pd

        from superset.sql.parse import SQLScript

        script = SQLScript(sql, self.db_engine_spec.engine)
        statements = list(script.statements)
        with self.get_sqla_engine(catalog=catalog, schema=schema) as engine:
            with closing(engine.raw_connection()) as conn:
                # Pre-session queries set the selected catalog/schema.
                for prequery in self.db_engine_spec.get_prequeries(
                    database=self,
                    catalog=catalog,
                    schema=schema,
                ):
                    pre_cursor = conn.cursor()
                    pre_cursor.execute(prequery)

                cursor = conn.cursor()
                df: pd.DataFrame | None = None
                for i, statement in enumerate(statements):
                    sql_ = self.mutate_sql_based_on_config(
                        statement.format(),
                        is_split=True,
                    )
                    self.db_engine_spec.execute(cursor, sql_, self)
                    rows = self.fetch_rows(cursor, i == len(statements) - 1)
                    if rows is not None:
                        df = self.load_into_dataframe(cursor.description, rows)

                if mutator:
                    mutated = mutator(df)
                    if mutated is not None:
                        df = mutated

                if df is None:
                    df = pd.DataFrame()
                return self.post_process_df(df)

    def fetch_rows(self, cursor: Any, last: bool) -> list[tuple[Any, ...]] | None:
        """Fetch rows for the final statement only.

        1:1 with ``superset_old/models/core.py:Database.fetch_rows``:
        intermediate statements are drained and discarded; only the last
        statement's rows are returned via the engine spec's ``fetch_data``.
        """
        if not last:
            cursor.fetchall()
            return None

        return self.db_engine_spec.fetch_data(cursor)

    def load_into_dataframe(
        self,
        description: Any,
        data: list[tuple[Any, ...]],
    ) -> Any:
        """Materialise raw DB-API rows into a normalised DataFrame.

        1:1 with ``superset_old/models/core.py:Database.load_into_dataframe``
        — goes through :class:`SupersetResultSet` so column de-duplication
        and type coercion match the rest of the codebase.
        """
        from superset.result_set import SupersetResultSet

        result_set = SupersetResultSet(data, description, self.db_engine_spec)
        return result_set.to_pandas_df()

    @staticmethod
    def post_process_df(df: Any) -> Any:
        """Serialise list/dict object columns to JSON strings.

        1:1 with ``superset_old/models/core.py:Database.post_process_df``.
        Note: ``json_dumps_w_dates`` comes from :mod:`superset.utils.json`
        (the module-level ``json`` in this file is the stdlib one).
        """
        import numpy as np
        import pandas as pd

        from superset.utils.json import json_dumps_w_dates

        def column_needs_conversion(df_series: Any) -> bool:
            return (
                not df_series.empty
                and isinstance(df_series, pd.Series)
                and isinstance(df_series[0], (list, dict))
            )

        for col, coltype in df.dtypes.to_dict().items():
            if coltype == np.object_ and column_needs_conversion(df[col]):
                df[col] = df[col].apply(json_dumps_w_dates)
        return df

    def select_star(
        self,
        table: Any,
        limit: int = 100,
        show_cols: bool = False,
        indent: bool = True,
        latest_partition: bool = False,
        cols: list[Any] | None = None,
    ) -> str:
        """Generate a ``select *`` statement in the proper dialect.

        1:1 with ``superset_old/models/core.py:Database.select_star`` —
        delegates to the engine spec, binding the engine to the table's
        catalog/schema so per-catalog dialects resolve correctly.
        """
        with self.get_sqla_engine(
            catalog=table.catalog, schema=table.schema
        ) as engine:
            return self.db_engine_spec.select_star(
                self,
                table,
                engine=engine,
                limit=limit,
                show_cols=show_cols,
                indent=indent,
                latest_partition=latest_partition,
                cols=cols,
            )

    def apply_limit_to_sql(
        self,
        sql: str,
        limit: int = 1000,
        force: bool = False,
    ) -> str:
        """Apply (or tighten) a LIMIT on the last statement of ``sql``.

        1:1 with ``superset_old/models/core.py:Database.apply_limit_to_sql``.
        The limit is only applied when it is stricter than the existing one
        (or when ``force`` is set), using the engine spec's ``limit_method``.
        """
        from superset.sql.parse import SQLScript

        script = SQLScript(sql, self.db_engine_spec.engine)
        statement = script.statements[-1]
        current_limit = statement.get_limit_value() or float("inf")

        if limit < current_limit or force:
            statement.set_limit_value(limit, self.db_engine_spec.limit_method)

        return script.format()

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

    def grains(self) -> tuple[TimeGrain, ...]:
        """Defines time granularity database-specific expressions.

        1:1 with ``superset_old/models/core.py:Database.grains`` — delegates to
        the engine spec's ``get_time_grains``. Consumed by the chart-data
        ``result_type=timegrains`` branch.
        """
        return self.db_engine_spec.get_time_grains()

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
        # SA 2.0's ``str(URL)`` masks the password as ``***`` regardless
        # of the value set on the URL object; ``render_as_string(hide_password=False)``
        # returns the actual replacement string, which we want to be the
        # ``XXXXXXXXXX`` PASSWORD_MASK so the original Apache Superset
        # contract (URI stored masked, real password on the ``password``
        # column) survives.
        self.sqlalchemy_uri = conn.render_as_string(hide_password=False)

    def safe_sqlalchemy_uri(self) -> str:
        return self.sqlalchemy_uri

    def get_effective_user(self, object_url: URL) -> str | None:
        """Get the effective user, especially during impersonation.

        1:1 with ``superset_old/models/core.py`` — prefer the current request
        user (from the async ``_current_user_ctx`` context-var via
        ``get_username()``), falling back to the URL's own username when
        impersonation is enabled.

        :param object_url: SQL Alchemy URL object
        :return: The effective username
        """
        from superset.utils.core import get_username

        return (
            username
            if (username := get_username())
            else object_url.username
            if self.impersonate_user
            else None
        )

    # ------------------------------------------------------------------
    # OAuth2 (1:1 with superset_old/models/core.py)
    # ------------------------------------------------------------------

    def is_oauth2_enabled(self) -> bool:
        """Return True when OAuth2 is enabled for this database.

        Mirrors ``Database.is_oauth2_enabled`` from the original — checks
        first for an in-row override in ``encrypted_extra``, then for a
        global engine-spec-level config.
        """
        try:
            client_config = self.get_oauth2_config()
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Invalid OAuth2 client configuration for database %s", self)
            client_config = None
        return client_config is not None or self.db_engine_spec.is_oauth2_enabled()

    def get_oauth2_config(self) -> dict[str, Any] | None:
        """Return the OAuth2 client configuration for this database.

        Per-database overrides live in ``encrypted_extra.oauth2_client_info``;
        falls back to the global engine-spec-level config.
        """
        encrypted_extra = self.get_encrypted_extra()
        if oauth2_client_info := encrypted_extra.get("oauth2_client_info"):
            # Mirror the marshmallow load_default behaviour: ensure all
            # required keys exist so consumers don't crash on a half-baked
            # in-row override.
            from superset.utils.oauth2 import get_default_oauth2_redirect_uri

            return {
                "id": oauth2_client_info["id"],
                "secret": oauth2_client_info["secret"],
                "scope": oauth2_client_info.get("scope", ""),
                "redirect_uri": oauth2_client_info.get(
                    "redirect_uri", get_default_oauth2_redirect_uri()
                ),
                "authorization_request_uri": oauth2_client_info[
                    "authorization_request_uri"
                ],
                "token_request_uri": oauth2_client_info["token_request_uri"],
                "request_content_type": oauth2_client_info.get(
                    "request_content_type", "json"
                ),
            }
        return self.db_engine_spec.get_oauth2_config()


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
    access_token = Column(encrypted_field_factory.create(Text), nullable=True)
    access_token_expiration = Column(DateTime, nullable=True)
    refresh_token = Column(encrypted_field_factory.create(Text), nullable=True)

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
