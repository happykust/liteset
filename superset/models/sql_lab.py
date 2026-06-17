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
"""SQL Lab models: Query, SavedQuery, TabState, TableSchema.

Pure SQLAlchemy -- no legacy WSGI dependencies.
"""

import enum
import json
from collections.abc import Hashable
from datetime import datetime
from typing import Any, TYPE_CHECKING

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
from sqlalchemy.orm import backref as sa_backref, relationship

from superset.models.connectors import AsyncQueryExecutionMixin
from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    ExploreMixin,
    ExtraJSONMixin,
    ImportExportMixin,
    LongText,
    MediumText,
)

if TYPE_CHECKING:
    from superset.models.connectors import TableColumn


# Enums


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


# Query


class Query(Base, ExtraJSONMixin, ExploreMixin, AsyncQueryExecutionMixin):
    """A SQL query executed in SQL Lab.

    In addition to being the persistence model for SQL Lab executions, a
    ``Query`` can act as a chart datasource (``datasource_type="query"``):
    it mixes in :class:`ExploreMixin` (the AST query builder) and
    :class:`AsyncQueryExecutionMixin` (the async build/execute pipeline).
    The chart-data ``columns`` are synthesized from ``self.extra["columns"]``
    (populated by SQL Lab on execution) rather than from a ``table_columns``
    relationship.
    """

    __tablename__ = "query"
    __table_args__ = (Index("ti_user_id_changed_on", "user_id", "changed_on"),)

    # Datasource type identifier used by QueryContext / chart-data.
    type = "query"
    query_language = "sql"

    id = Column(Integer, primary_key=True)
    client_id = Column(String(11), unique=True, nullable=False)
    database_id = Column(Integer, ForeignKey("dbs.id"), nullable=False)
    tmp_table_name = Column(String(256))
    tmp_schema_name = Column(String(256))
    user_id = Column(Integer, ForeignKey("ab_user.id"), nullable=True)
    status = Column(String(16), default="pending")
    tab_name = Column(String(256))
    sql_editor_id = Column(String(256), index=True)
    schema = Column(String(256))
    catalog = Column(String(256), nullable=True, default=None)
    sql = Column(LongText())
    select_sql = Column(LongText())
    executed_sql = Column(LongText())
    limit = Column(Integer)
    # ``native_enum=False`` for asyncpg compatibility — see note on
    # ``Tag.type`` / ``TaggedObject.object_type`` / ``RLSFilter.filter_type``.
    # The metadata DB stores the column as VARCHAR (or as a native PG ENUM
    # ``limitingfactor`` on installations that ran migration 0002); both
    # cases work with text-based bind values.
    limiting_factor = Column(
        Enum(LimitingFactor, native_enum=False), server_default="UNKNOWN"
    )
    select_as_cta = Column(Boolean)
    select_as_cta_used = Column(Boolean, default=False)
    ctas_method = Column(String(16), default="TABLE")
    progress = Column(Integer, default=0)
    rows = Column(Integer)
    error_message = Column(Text)
    results_key = Column(String(64), index=True)
    start_time = Column(Numeric(precision=20, scale=6))
    start_running_time = Column(Numeric(precision=20, scale=6))
    end_time = Column(Numeric(precision=20, scale=6))
    end_result_backend_time = Column(Numeric(precision=20, scale=6))
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
        backref=sa_backref("queries", cascade="all, delete-orphan"),
    )
    user = relationship(
        "User",
        foreign_keys=[user_id],
    )

    @property
    def tracking_url(self) -> str | None:
        """Transform the tracking URL via ``TRACKING_URL_TRANSFORMER`` at read time.

        The exact URL may depend on query properties (execution/finish time),
        so the transform runs on read, not on store. For backward compatibility
        a transformer may take only ``url`` or ``(url, query)``. Falls back to
        the raw value when no transformer is configured (the default).
        """
        import inspect

        from superset import config as _config

        url = self.tracking_url_raw
        # Common case: no tracking URL (only Hive/Presto/Trino set one) —
        # skip the settings construction entirely.
        if not url:
            return url
        try:
            settings = _config.SupersetSettings()
            transform = getattr(settings, "tracking_url_transformer", None)
        except Exception:  # noqa: BLE001
            transform = None
        if transform:
            sig = inspect.signature(transform)
            args = [url, self][: len(sig.parameters)]
            url = transform(*args)
        return url

    def to_dict(self) -> dict[str, Any]:
        """Serialize query to a dict matching the original camelCase contract.

        The frontend relies on these exact keys (dbId, endDttm, startDttm,
        sqlEditorId, etc.) so we must preserve the original naming.
        """
        # Avoid lazy-loading relationships in async context.
        # Use sa.inspect() to check if attributes are already loaded.
        from sqlalchemy import inspect as sa_inspect

        state = sa_inspect(self)

        # Use a distinct local name to avoid shadowing the imported
        # ``user_label`` function (which would cause TypeError on line 2).
        from superset.utils.core import user_label as _user_label_fn

        _user_label: str | None = None
        if "user" not in state.unloaded and self.user:
            _user_label = _user_label_fn(self.user)

        db_name = None
        if "database" not in state.unloaded and self.database is not None:
            db_name = self.database.database_name

        # None (JSON null), not "" — "" is not a valid DateTime for clients
        # that re-validate; upstream serialised ``changed_on.isoformat()``
        # unconditionally (changed_on is practically never NULL).
        changed_on_iso = None
        if self.changed_on is not None:
            changed_on_iso = self.changed_on.isoformat()

        return {
            "changed_on": changed_on_iso,
            "dbId": self.database_id,
            "db": db_name,
            "endDttm": self.end_time,
            "errorMessage": self.error_message,
            "executedSql": self.executed_sql,
            "id": self.client_id,
            "queryId": self.id,
            "limit": self.limit,
            "limitingFactor": (
                self.limiting_factor.value
                if isinstance(self.limiting_factor, LimitingFactor)
                else self.limiting_factor
            ),
            "progress": self.progress,
            "rows": self.rows,
            "catalog": self.catalog,
            "schema": self.schema,
            "ctas": self.select_as_cta,
            "serverId": self.id,
            "sql": self.sql,
            "sqlEditorId": self.sql_editor_id,
            "startDttm": self.start_time,
            "state": (self.status or "").lower(),
            "tab": self.tab_name,
            "tempSchema": self.tmp_schema_name,
            "tempTable": self.tmp_table_name,
            "userId": self.user_id,
            "user": _user_label,
            "resultsKey": self.results_key,
            "trackingUrl": self.tracking_url,
            "extra": self.extra,
        }

    # ------------------------------------------------------------------
    # Datasource interface (datasource_type="query")
    #
    # These shims let a SQL Lab result act as a chart datasource via the inherited
    # :class:`ExploreMixin` query builder and :class:`AsyncQueryExecutionMixin`
    # execution pipeline.
    # ------------------------------------------------------------------

    def get_template_processor(self, **kwargs: Any) -> Any:
        """Return a Jinja template processor bound to this query.

        Passes ``query=self`` (not ``table=self``) so the ``{{ query }}`` Jinja
        context resolves to this SQL Lab query.
        """
        from superset.jinja_context import get_template_processor

        return get_template_processor(query=self, database=self.database, **kwargs)

    @property
    def columns(self) -> list["TableColumn"]:
        """Synthesize transient ``TableColumn`` objects from ``extra``.

        SQL Lab stores the result-set column metadata in ``extra["columns"]`` on
        execution; one ``TableColumn`` is materialised per entry. The objects are
        transient — never added to a session, so no flush is ever triggered. Keys are
        read defensively (``dict.get``) so a query whose stored ``extra`` predates a
        column-metadata field does not 500.
        """
        from superset.models.connectors import TableColumn

        return [
            TableColumn(
                column_name=col.get("column_name"),
                is_dttm=col.get("is_dttm", False),
                filterable=True,
                groupby=True,
                type=col.get("type"),
            )
            for col in self.extra.get("columns", [])
        ]

    @property
    def column_names(self) -> list[Any]:
        return [col.column_name for col in self.columns]

    def get_column(self, column_name: str | None) -> "TableColumn | None":
        if not column_name:
            return None
        for col in self.columns:
            if col.column_name == column_name:
                return col
        return None

    @property
    def db_extra(self) -> dict[str, Any] | None:
        return None

    @property
    def db_engine_spec(self) -> Any:
        return self.database.db_engine_spec

    @property
    def data(self) -> dict[str, Any]:
        from superset.i18n import gettext as __

        order_by_choices = []
        for col in self.columns:
            column_name = str(col.column_name or "")
            order_by_choices.append(
                (json.dumps([column_name, True]), f"{column_name} " + __("[asc]"))
            )
            order_by_choices.append(
                (json.dumps([column_name, False]), f"{column_name} " + __("[desc]"))
            )

        return {
            "time_grain_sqla": [
                (g.duration, g.name) for g in self.database.grains() or []
            ],
            "filter_select": True,
            "name": self.tab_name,
            "columns": [o.data for o in self.columns],
            "metrics": [],
            "id": self.id,
            "type": self.type,
            "sql": self.sql,
            "owners": self.owners_data,
            "database": {"id": self.database_id, "backend": self.database.backend},
            "order_by_choices": order_by_choices,
            "catalog": self.catalog,
            "schema": self.schema,
            "verbose_map": {},
        }

    @property
    def owners_data(self) -> list[dict[str, Any]]:
        return []

    @property
    def uid(self) -> str:
        return f"{self.id}__{self.type}"

    @property
    def is_rls_supported(self) -> bool:
        return False

    @property
    def cache_timeout(self) -> int:
        return 0

    @property
    def offset(self) -> int:
        return 0

    @property
    def main_dttm_col(self) -> str | None:
        """Return the first datetime column name, or None.

        Port delta: the port's ``columns`` yields ``TableColumn`` objects
        (attribute access), so this iterates with ``col.is_dttm`` /
        ``col.column_name`` — consistent with :attr:`dttm_cols` — rather
        than upstream's ``col.get("is_dttm")`` dict access (which would
        raise on the object-valued columns produced here).
        """
        for col in self.columns:
            if col.is_dttm:
                return col.column_name
        return None

    @property
    def dttm_cols(self) -> list[Any]:
        return [col.column_name for col in self.columns if col.is_dttm]

    @property
    def schema_perm(self) -> str:
        return f"{self.database.database_name}.{self.schema}"

    @property
    def perm(self) -> str:
        return f"[{self.database.database_name}].[{self.tab_name}](id:{self.id})"

    @property
    def default_endpoint(self) -> str:
        return ""

    def get_extra_cache_keys(self, query_obj: dict[str, Any]) -> list[Hashable]:
        return []

    def adhoc_column_to_sqla(
        self,
        col: dict[str, Any],
        force_type_check: bool = False,
        template_processor: Any | None = None,
    ) -> Any:
        """Turn an adhoc column into a SQLAlchemy column."""
        from sqlalchemy.sql.elements import literal_column

        from superset.utils.column import get_column_name

        label = get_column_name(col)
        expression = self._process_sql_expression(
            expression=col["sqlExpression"],
            database_id=self.database_id,
            engine=self.database.backend,
            schema=self.schema,
            template_processor=template_processor,
        )
        sqla_column = literal_column(expression)
        return self.make_sqla_column_compatible(sqla_column, label)


# SavedQuery


class SavedQuery(Base, AuditMixinNullable, ExtraJSONMixin, ImportExportMixin):
    """A saved SQL query."""

    __tablename__ = "saved_query"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("ab_user.id"), nullable=True)
    db_id = Column(Integer, ForeignKey("dbs.id"), nullable=True)
    schema = Column(String(128))
    catalog = Column(String(256), nullable=True, default=None)
    label = Column(String(256))
    description = Column(Text)
    sql = Column(MediumText())
    template_parameters = Column(Text)
    rows = Column(Integer)
    last_run = Column(DateTime)

    def __repr__(self) -> str:
        return str(self.label)

    # -- relationships --------------------------------------------------------

    user = relationship(
        "User",
        foreign_keys=[user_id],
        backref=sa_backref("saved_queries", cascade="all, delete-orphan"),
    )
    database = relationship(
        "Database",
        foreign_keys=[db_id],
        backref=sa_backref("saved_queries", cascade="all, delete-orphan"),
    )
    tags = relationship(
        "Tag",
        secondary="tagged_object",
        overlaps="objects,tag,tags",
        primaryjoin="and_(SavedQuery.id == foreign(TaggedObject.object_id), "
        "TaggedObject.object_type == 'query')",
        secondaryjoin="Tag.id == foreign(TaggedObject.tag_id)",
        viewonly=True,
    )

    export_parent = "database"
    export_fields = [
        "catalog",
        "schema",
        "label",
        "description",
        "sql",
    ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
        }

    def url(self) -> str:
        return f"/sqllab?savedQueryId={self.id}"


# TabState


class TabState(AuditMixinNullable, ExtraJSONMixin, Base):
    """Persisted SQL Lab tab state."""

    __tablename__ = "tab_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("ab_user.id"), nullable=True)
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
        passive_deletes=True,
    )
    latest_query = relationship(
        "Query",
        foreign_keys=[latest_query_id],
    )
    saved_query = relationship(
        "SavedQuery",
        foreign_keys=[saved_query_id],
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "label": self.label,
            "active": self.active,
            "database_id": self.database_id,
            "catalog": self.catalog,
            "schema": self.schema,
            "table_schemas": [ts.to_dict() for ts in self.table_schemas],
            "sql": self.sql,
            "query_limit": self.query_limit,
            "latest_query": self.latest_query.to_dict() if self.latest_query else None,
            "autorun": self.autorun,
            "template_params": self.template_params,
            "hide_left_bar": self.hide_left_bar,
            "saved_query": self.saved_query.to_dict() if self.saved_query else None,
            "extra_json": self.extra,
        }


# TableSchema


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
        nullable=False,
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

    def to_dict(self) -> dict[str, Any]:
        try:
            description = json.loads(self.description)
        except (json.JSONDecodeError, TypeError):
            description = None
        return {
            "id": self.id,
            "tab_state_id": self.tab_state_id,
            "database_id": self.database_id,
            "catalog": self.catalog,
            "schema": self.schema,
            "table": self.table,
            "description": description,
            "expanded": self.expanded,
        }
