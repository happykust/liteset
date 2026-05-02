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

"""Row-Level Security utilities — both async (chart-data pipeline) and
synchronous (SQL Lab + virtual-dataset injection).

The async path (:func:`compose_rls_where_clauses`) is used by the
``query_context_processor`` and ``controllers/datasource``.  The sync
path (:func:`compose_rls_text_clauses`, :func:`get_predicates_for_table`,
:func:`apply_rls`) is used by ``models.helpers``: ``validate_adhoc_subquery``
and ``_get_virtual_from_clause`` parse user-provided SQL and rely on
synchronous RLS injection.

Both paths share the same business logic: filters with the same
``group_key`` are OR'ed together within a group, groups are AND'ed at
the call site, every ``clause`` is run through Jinja templating.
"""

from __future__ import annotations

import functools
import logging
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, TYPE_CHECKING

from sqlalchemy import and_, create_engine, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import text
from sqlalchemy.sql.elements import ClauseElement

from superset.sql.parse import Table

if TYPE_CHECKING:
    from collections.abc import Iterator

    from superset.models.core import Database
    from superset.sql.parse import BaseSQLStatement

logger = logging.getLogger(__name__)


# ===========================================================================
# Async path — used by chart data pipeline (query_context_processor,
# controllers/datasource, controllers/chart preview/samples).
# ===========================================================================


async def compose_rls_where_clauses(  # noqa: C901  # complex business logic
    table: Any,
    *,
    user: Any,
    security_manager: Any,
    template_processor: Any | None = None,
) -> list[ClauseElement]:
    """Build SQLAlchemy clause objects for RLS filters on ``table``.

    Ports the business logic of
    ``superset_old.connectors.sqla.models.BaseDatasource.get_sqla_row_level_filters``
    (lines 654-697) **including the return type**: each clause is a
    SQLAlchemy ``TextClause`` (standalone filter) or ``BooleanClauseList``
    (``or_(...)`` of grouped filters), both subclasses of ``ClauseElement``.

    The caller integrates them via ``query.where(and_(*clauses))`` (or by
    compiling each one against the database's dialect for raw-SQL
    pipelines such as ``SqlaTable._build_sql``).  Compiling through the
    target dialect gives automatic identifier quoting and dialect-specific
    translation — exactly what the original SQLAlchemy AST path provides.

    - filters with the same ``group_key`` are OR-ed together within their
      group via ``or_(*clauses)``; each group becomes one ``BooleanClauseList``
    - filters without a ``group_key`` are returned as standalone ``TextClause``
    - each ``clause`` string is run through Jinja's ``process_template``
      (so e.g. ``{{ current_user_id() }}`` is resolved at query time)
    - if ``EMBEDDED_SUPERSET`` is on, guest-token RLS clauses are appended
      (also Jinja-processed)
    - rendering errors surface as ``QueryObjectValidationError``
    """
    from jinja2 import TemplateError

    from superset.exceptions import (
        QueryObjectValidationError,
        SupersetSyntaxErrorException,
    )

    if template_processor is None:
        from superset.jinja_context import get_template_processor

        database = getattr(table, "database", None)
        if database is not None:
            template_processor = get_template_processor(database=database, table=table)

    def _process(clause: str) -> str:
        if template_processor is None or not clause:
            return clause
        return template_processor.process_template(clause)

    all_filters: list[ClauseElement] = []
    filter_groups: dict[str | int, list[ClauseElement]] = defaultdict(list)

    try:
        rls_filters = await security_manager.get_rls_filters(table, user=user)
        for filter_ in rls_filters:
            rendered = _process(filter_.clause)
            clause = text(f"({rendered})")
            if filter_.group_key:
                filter_groups[filter_.group_key].append(clause)
            else:
                all_filters.append(clause)

        if _embedded_superset_enabled():
            guest_rules = security_manager.get_guest_rls_filters(table, user=user)
            for rule in guest_rules:
                rendered = _process(rule.get("clause", ""))
                if rendered:
                    all_filters.append(text(f"({rendered})"))

        # OR within a group; groups themselves are AND-ed by the caller.
        for clauses in filter_groups.values():
            all_filters.append(or_(*clauses))
        return all_filters
    except (TemplateError, SupersetSyntaxErrorException) as ex:
        msg = getattr(ex, "message", str(ex))
        raise QueryObjectValidationError(
            f"Error in jinja expression in RLS filters: {msg}"
        ) from ex


def _embedded_superset_enabled() -> bool:
    """Return whether the ``EMBEDDED_SUPERSET`` feature flag is on.

    Returns ``False`` if the feature-flag module isn't importable (e.g.
    very early in app boot before the manager is registered) — the only
    legitimate failure mode for this lookup. All other errors must
    surface so misconfiguration is loud.
    """
    try:
        from superset.utils.feature_flags import feature_flag_manager
    except ImportError:
        return False
    return bool(feature_flag_manager.is_feature_enabled("EMBEDDED_SUPERSET"))


# ===========================================================================
# Sync path — used by SQL Lab pipeline (validate_adhoc_subquery,
# _get_virtual_from_clause). Original Superset uses Flask-SQLAlchemy's
# ``db.session``; in Liteset we open a dedicated sync session against the
# metadata DB, mirroring ``jinja_context._sync_find_dataset``.
# ===========================================================================


# Map async SQLAlchemy drivers to their sync equivalents. Mirrors
# ``superset.db.engine._ASYNC_TO_SYNC_DRIVERS`` and the table in
# ``superset/cli/db.py`` so ``create_engine`` accepts the metadata DSN
# even when ``LITESET_SQLALCHEMY_DATABASE_URI`` points at an async
# driver (which it always does at runtime).
_ASYNC_TO_SYNC_DRIVERS: dict[str, str] = {
    "postgresql+asyncpg://": "postgresql+psycopg2://",
    "mysql+aiomysql://": "mysql+pymysql://",
    "mysql+asyncmy://": "mysql+pymysql://",
    "sqlite+aiosqlite://": "sqlite://",
}


def _to_sync_uri(uri: str) -> str:
    """Convert an async SQLAlchemy URI to its sync equivalent.

    Required because :func:`create_engine` (sync) refuses async drivers
    such as ``asyncpg`` with ``InvalidArgumentError: The asyncio
    extension requires an async driver``. The runtime metadata DSN is
    always async (asyncpg/aiomysql/aiosqlite), but RLS sync paths and
    Alembic-style migrations need a sync engine.
    """
    for src, dst in _ASYNC_TO_SYNC_DRIVERS.items():
        if uri.startswith(src):
            return uri.replace(src, dst, 1)
    return uri


@functools.lru_cache(maxsize=1)
def _cached_settings() -> Any:
    """Return a process-wide cached :class:`SupersetSettings` instance.

    ``SupersetSettings`` performs file I/O and env scanning on every
    instantiation; sync RLS paths call into config on every clause
    evaluation, so caching the instance avoids hot-path overhead.
    """
    from superset.config import SupersetSettings

    return SupersetSettings()  # type: ignore[call-arg]


@functools.lru_cache(maxsize=1)
def _metadata_sync_engine() -> Any:
    """Return a process-wide cached sync engine for the metadata DB.

    The async query pipeline uses ``AsyncSession``; sync code paths
    (SQL Lab adhoc subquery validation, virtual-dataset RLS injection)
    need a regular ``Session``.  Caching the engine avoids creating a
    new one on every call.

    The runtime DSN uses an async driver (e.g. ``postgresql+asyncpg://``);
    we strip the async marker via :func:`_to_sync_uri` before handing it
    to the sync :func:`create_engine`.
    """
    settings = _cached_settings()
    sync_uri = _to_sync_uri(str(settings.sqlalchemy_database_uri))
    return create_engine(sync_uri)


@contextmanager
def _metadata_sync_session() -> Iterator[Session]:
    """Yield a sync ``Session`` bound to the metadata DB."""
    session = Session(_metadata_sync_engine())
    try:
        yield session
    finally:
        session.close()


def _sync_resolve_user_role_ids(user: Any) -> list[int] | None:
    """Sync mirror of ``AsyncSecurityManager._resolve_user_roles_for_rls``.

    Returns the list of role ids to use for RLS filter selection:

    * Authenticated user → ids of their roles.
    * Anonymous / missing user → ``[Public role id]`` if
      ``AUTH_ROLE_PUBLIC`` resolves; otherwise ``None`` (caller returns
      ``[]``, matching the original behaviour when no Public role is
      configured).
    """
    is_anonymous = (
        user is None
        or getattr(user, "is_anonymous", False)
        or not getattr(user, "is_authenticated", True)
    )
    if not is_anonymous:
        return [r.id for r in (getattr(user, "roles", []) or [])]

    settings = _cached_settings()
    public_role_name = settings.auth_role_public or "Public"

    if not public_role_name:
        return None

    # Resolve the role through the metadata sync session, mirroring
    # ``AsyncSecurityDAO.get_role_by_name``.
    from superset.models.security import Role

    with _metadata_sync_session() as session:
        role = (
            session.execute(select(Role).where(Role.name == public_role_name))
            .scalars()
            .one_or_none()
        )
        if role is None:
            return None
        return [role.id]


def _sync_get_rls_filters_for_user(
    table_id: int,
    user_role_ids: list[int],
) -> list[Any]:
    """Synchronous port of
    ``superset_old.security.manager.SupersetSecurityManager.get_rls_filters``.

    Returns a list of ``RowLevelSecurityFilter`` objects matching:
    - REGULAR filters where ``user_role_ids`` ∩ filter.roles ≠ ∅
    - BASE filters where ``user_role_ids`` ∩ filter.roles = ∅

    Anonymous users (no roles) get no REGULAR filters and *all* BASE filters
    that target the table — exactly as in the original.
    """
    from superset.models.connectors import (
        RLSFilterRoles,
        RLSFilterTables,
        RowLevelSecurityFilter,
    )
    from superset.utils.core import RowLevelSecurityFilterType

    with _metadata_sync_session() as session:
        filter_tables_sq = select(RLSFilterTables.c.rls_filter_id).where(
            RLSFilterTables.c.table_id == table_id
        )
        regular_filter_roles_sq = (
            select(RLSFilterRoles.c.rls_filter_id)
            .join(
                RowLevelSecurityFilter,
                RLSFilterRoles.c.rls_filter_id == RowLevelSecurityFilter.id,
            )
            .where(
                RowLevelSecurityFilter.filter_type == RowLevelSecurityFilterType.REGULAR
            )
            .where(RLSFilterRoles.c.role_id.in_(user_role_ids))
        )
        base_filter_roles_sq = (
            select(RLSFilterRoles.c.rls_filter_id)
            .join(
                RowLevelSecurityFilter,
                RLSFilterRoles.c.rls_filter_id == RowLevelSecurityFilter.id,
            )
            .where(
                RowLevelSecurityFilter.filter_type == RowLevelSecurityFilterType.BASE
            )
            .where(RLSFilterRoles.c.role_id.in_(user_role_ids))
        )
        # Select only the columns the caller actually needs — returning
        # ORM objects bound to a sync session that the caller closes
        # immediately would lazy-load via a stale session on attribute
        # access. Row tuples carry no ORM state so they survive past
        # ``session.close()`` and ``f.clause`` / ``f.group_key`` resolve
        # via tuple-attribute access just like the ORM equivalent.
        stmt = (
            select(
                RowLevelSecurityFilter.id,
                RowLevelSecurityFilter.group_key,
                RowLevelSecurityFilter.clause,
            )
            .where(RowLevelSecurityFilter.id.in_(filter_tables_sq))
            .where(
                or_(
                    and_(
                        RowLevelSecurityFilter.filter_type
                        == RowLevelSecurityFilterType.REGULAR,
                        RowLevelSecurityFilter.id.in_(regular_filter_roles_sq),
                    ),
                    and_(
                        RowLevelSecurityFilter.filter_type
                        == RowLevelSecurityFilterType.BASE,
                        RowLevelSecurityFilter.id.notin_(base_filter_roles_sq),
                    ),
                )
            )
        )
        return list(session.execute(stmt).all())


def compose_rls_text_clauses(  # noqa: C901  # complex business logic
    table: Any,
    template_processor: Any | None = None,
) -> list[Any]:
    """Synchronous port of
    ``superset_old.connectors.sqla.models.BaseDatasource.get_sqla_row_level_filters``.

    Returns a list of SQLAlchemy ``TextClause`` / ``ColumnElement`` objects
    that can be AND-ed together at the call site (``where(and_(*clauses))``)
    or compiled to literal SQL strings for the SQL-AST RLS injection
    (``apply_rls`` → ``parse_predicate``).

    Logic mirrors :func:`compose_rls_where_clauses` exactly:
    - ``group_key`` filters OR-ed within group, AND-ed across groups
    - Jinja ``process_template`` applied to every clause
    - ``EMBEDDED_SUPERSET`` guest RLS appended when feature flag is on

    Current user is resolved from
    :func:`superset.utils.core.get_current_user` — set by the auth
    middleware on every request.
    """
    from jinja2 import TemplateError

    from superset.exceptions import (
        QueryObjectValidationError,
        SupersetSyntaxErrorException,
    )
    from superset.utils.core import get_current_user

    user = get_current_user()
    user_role_ids = _sync_resolve_user_role_ids(user)
    if user_role_ids is None:
        return []
    rls_filters = _sync_get_rls_filters_for_user(table.id, user_role_ids)

    # Mirror the original which always renders Jinja:
    # ``template_processor = template_processor or self.get_template_processor()``.
    # Don't swallow init errors here — the original Superset doesn't, and a
    # template-processor construction failure indicates a broken Database
    # configuration that callers must surface (the outer try/except below
    # already maps Jinja-rendering errors to QueryObjectValidationError).
    if template_processor is None:
        from superset.jinja_context import get_template_processor

        database = getattr(table, "database", None)
        if database is not None:
            template_processor = get_template_processor(database=database, table=table)

    all_filters: list[Any] = []
    filter_groups: dict[Any, list[Any]] = defaultdict(list)

    try:
        for filter_ in rls_filters:
            clause_str = filter_.clause
            if template_processor is not None:
                clause_str = template_processor.process_template(clause_str)
            text_clause = text(f"({clause_str})")
            if filter_.group_key:
                filter_groups[filter_.group_key].append(text_clause)
            else:
                all_filters.append(text_clause)

        # Guest token RLS — only relevant when EMBEDDED_SUPERSET is on
        # *and* the current user is a guest. ``get_current_user``
        # returns the guest user in that case (the auth middleware
        # decodes the guest token).
        if _embedded_superset_enabled():
            for clause_str in _sync_get_guest_rls_clauses(user, table):
                if template_processor is not None and clause_str:
                    clause_str = template_processor.process_template(clause_str)
                if clause_str:
                    all_filters.append(text(f"({clause_str})"))

        for clauses in filter_groups.values():
            all_filters.append(or_(*clauses))
        return all_filters
    except (TemplateError, SupersetSyntaxErrorException) as ex:
        msg = getattr(ex, "message", str(ex))
        raise QueryObjectValidationError(
            f"Error in jinja expression in RLS filters: {msg}"
        ) from ex


def _sync_get_guest_rls_clauses(user: Any, table: Any) -> list[str]:
    """Extract guest-token RLS clauses for the dataset.

    Mirrors
    ``superset_old.security.manager.SupersetSecurityManager.get_guest_rls_filters``
    but in sync context — the guest user's RLS rules live on the user
    object itself (decoded from the guest token by auth middleware).

    Original ``get_guest_rls_filters`` short-circuits if the current user
    is not a guest user (``get_current_guest_user_if_guest`` returns
    ``None``); we mirror that gate here. ``user.is_guest`` is the
    canonical marker (see :class:`superset.security.guest.GuestUser` and
    ``AsyncSecurityManager.is_guest_user``).
    """
    if not getattr(user, "is_guest", False):
        return []
    rls_rules: list[dict[str, Any]] = getattr(user, "rls_rules", []) or []
    if not rls_rules:
        return []
    table_id_str = str(getattr(table, "id", ""))
    return [
        rule.get("clause", "")
        for rule in rls_rules
        if not rule.get("dataset") or str(rule.get("dataset")) == table_id_str
    ]


# ===========================================================================
# SQL-AST RLS injection — used by SQL Lab for adhoc/virtual SQL.
# ===========================================================================


def apply_rls(
    database: Database,
    catalog: str | None,
    schema: str,
    parsed_statement: BaseSQLStatement[Any],
) -> None:
    """Modify ``parsed_statement`` inplace to inject RLS predicates.

    Two strategies (chosen by ``database.db_engine_spec.get_rls_method()``):
    - replace each table reference with a ``(SELECT * FROM t WHERE rls)``
      subquery (safer, but not all engines support it),
    - or append the RLS predicates to the ``WHERE`` clause (default).
    """
    method = database.db_engine_spec.get_rls_method()

    predicates: dict[Table, list[Any]] = {}
    for tbl in parsed_statement.tables:
        tbl = tbl.qualify(catalog=catalog, schema=schema)
        predicates[tbl] = [
            parsed_statement.parse_predicate(predicate)
            for predicate in get_predicates_for_table(
                tbl,
                database,
                database.get_default_catalog(),
            )
            if predicate
        ]

    parsed_statement.apply_rls(catalog, schema, predicates, method)


def get_predicates_for_table(
    table: Table,
    database: Database,
    default_catalog: str | None,
) -> list[str]:
    """Return RLS predicates (as compiled SQL strings) for a table.

    Used by :func:`apply_rls` for SQL Lab and virtual-dataset RLS
    injection.  Looks up the dataset by (database, catalog, schema,
    table_name) and asks it for its RLS clauses; each clause is
    compiled to a literal SQL string so the parser can reinsert it
    into the AST.
    """
    from superset.models.connectors import SqlaTable

    catalog_predicate = SqlaTable.catalog == table.catalog
    if table.catalog and table.catalog == default_catalog:
        catalog_predicate = or_(
            catalog_predicate,
            SqlaTable.catalog.is_(None),
        )

    with _metadata_sync_session() as session:
        dataset = (
            session.execute(
                select(SqlaTable).where(
                    and_(
                        SqlaTable.database_id == database.id,
                        catalog_predicate,
                        SqlaTable.schema == table.schema,
                        SqlaTable.table_name == table.table,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if not dataset:
            return []
        # Resolve ORM relationships (``database``) inside the active
        # session — ``get_sqla_row_level_filters`` opens its own session
        # for the RLS query, but the *caller* may walk ``dataset.database``
        # for the SQL dialect, etc.
        _ = dataset.database  # eager load while session is open
        rls_clauses = dataset.get_sqla_row_level_filters()

    return [
        str(
            predicate.compile(
                dialect=database.get_dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        for predicate in rls_clauses
    ]


def collect_rls_predicates_for_sql(
    sql: str,
    database: Database,
    catalog: str | None,
    schema: str,
) -> list[str]:
    """Collect all RLS predicates that would apply to tables in ``sql``.

    Used for cache key generation for virtual datasets so different
    users with different RLS rules get different cache entries.
    """
    from superset.sql.parse import SQLScript

    try:
        parsed_script = SQLScript(sql, engine=database.db_engine_spec.engine)
        tables = {
            tbl.qualify(catalog=catalog, schema=schema)
            for statement in parsed_script.statements
            for tbl in statement.tables
        }
        default_catalog = database.get_default_catalog()
        return sorted(
            {
                predicate
                for tbl in tables
                for predicate in get_predicates_for_table(
                    tbl,
                    database,
                    default_catalog,
                )
            }
        )
    except Exception:  # noqa: BLE001
        # If we can't parse the SQL, return empty list — RLS application
        # failure must not break caching.
        return []
