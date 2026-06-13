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
"""Sync execution context for example data loading.

Provides module-level ``session`` and ``engine`` that example modules
import instead of the original ``from superset import db``.

The context is initialised once by :func:`init` (called from the CLI
command) and torn down by :func:`teardown`.
"""

from __future__ import annotations

import enum
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state — set by init(), consumed by example modules
# ---------------------------------------------------------------------------
session: Session = None  # type: ignore[assignment]
engine: Engine = None  # type: ignore[assignment]
row_limit: int = 50_000
base_dir: str = ""

_session_factory: sessionmaker[Session] | None = None

# ---------------------------------------------------------------------------
# Async-to-sync driver mapping (mirrors db/engine.py)
# ---------------------------------------------------------------------------
_ASYNC_TO_SYNC_DRIVERS: dict[str, str] = {
    "postgresql+asyncpg": "postgresql+psycopg2",
    "sqlite+aiosqlite": "sqlite",
    "mysql+aiomysql": "mysql+pymysql",
    "mysql+asyncmy": "mysql+pymysql",
}


def _to_sync_uri(uri: str) -> str:
    """Convert an async SQLAlchemy URI to its sync equivalent."""
    for async_pfx, sync_pfx in _ASYNC_TO_SYNC_DRIVERS.items():
        if uri.startswith(async_pfx):
            return uri.replace(async_pfx, sync_pfx, 1)
    return uri


# ---------------------------------------------------------------------------
# DatasourceType (needed by example modules; originally in utils/core.py)
# ---------------------------------------------------------------------------
class DatasourceType(str, enum.Enum):
    TABLE = "table"
    DATASET = "dataset"
    QUERY = "query"
    SAVEDQUERY = "saved_query"
    VIEW = "view"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EXAMPLES_DB_UUID = "a2dc77af-e654-49bb-b321-40f6b559a1ee"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def init() -> None:
    """Create the sync engine and session for metadata operations."""
    global session, engine, row_limit, base_dir, _session_factory  # noqa: PLW0603

    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    sync_uri = _to_sync_uri(settings.sqlalchemy_database_uri)
    row_limit = settings.row_limit
    base_dir = str(Path(__file__).resolve().parent.parent)

    engine = create_engine(sync_uri)
    _session_factory = sessionmaker(bind=engine)
    session = _session_factory()
    logger.info("Examples context initialised (engine=%s)", engine.url)


def teardown() -> None:
    """Close session and dispose engine."""
    global session, engine, _session_factory  # noqa: PLW0603
    if session is not None:
        session.close()
        session = None  # type: ignore[assignment]
    if engine is not None:
        engine.dispose()
        engine = None  # type: ignore[assignment]
    _session_factory = None


def commit() -> None:
    """Commit the current session."""
    if session is not None:
        session.commit()


# ---------------------------------------------------------------------------
# Example-database helpers
# ---------------------------------------------------------------------------
def get_example_database() -> Any:
    """Return (or create) the ``examples`` Database model record.

    The examples database stores the actual example data tables.
    Its URI defaults to the main metadata database URI.
    """
    from superset.models.core import Database

    db = session.query(Database).filter_by(database_name="examples").first()
    if db is None:
        import uuid as _uuid

        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        examples_uri = (
            os.environ.get("LITESET_SQLALCHEMY_EXAMPLES_URI")
            or settings.sqlalchemy_examples_uri
            or settings.sqlalchemy_database_uri
        )

        db = Database(
            database_name="examples",
            sqlalchemy_uri=examples_uri,
            uuid=_uuid.UUID(EXAMPLES_DB_UUID),
        )
        session.add(db)
        session.flush()
        logger.info("Created examples database record (id=%s)", db.id)
    return db


def get_example_engine(database: Any) -> Engine:
    """Return a sync engine for loading example data.

    When the examples database URI matches the metadata URI (the common
    case), reuse the already-initialised module-level engine to avoid
    credential / connectivity issues.
    """
    db_uri = _to_sync_uri(database.sqlalchemy_uri)
    if engine is not None and str(engine.url) == db_uri:
        return engine
    return create_engine(db_uri)


@contextmanager
def example_engine(database: Any) -> Iterator[Engine]:
    """Context-managed sync engine for loading data into example tables."""
    eng = get_example_engine(database)
    try:
        yield eng
    finally:
        # Don't dispose the shared metadata engine
        if eng is not engine:
            eng.dispose()


def has_table(eng: Engine, table_name: str, schema: str | None = None) -> bool:
    """Check if a table exists using SQLAlchemy inspect."""
    return inspect(eng).has_table(table_name, schema=schema)


def get_schema(eng: Engine) -> str | None:
    """Return default schema for an engine."""
    return inspect(eng).default_schema_name


def get_backend(database: Any) -> str:
    """Extract backend name (postgresql, mysql, sqlite, etc.) from URI."""
    uri = database.sqlalchemy_uri or ""
    return uri.split("://")[0].split("+")[0] if "://" in uri else ""


def fetch_table_metadata(tbl: Any, eng: Engine) -> None:
    """Populate SqlaTable.columns from physical table introspection.

    Replacement for the original ``tbl.fetch_metadata()`` which relied
    on the upstream framework infrastructure. Introspects the actual database
    table and creates/updates ``TableColumn`` records, matching the
    original behavior.
    """
    from superset.models.connectors import SqlMetric, TableColumn

    table_name = tbl.table_name
    schema_name = tbl.schema

    try:
        cols_info = inspect(eng).get_columns(table_name, schema=schema_name)
    except Exception:
        logger.warning(
            "Could not introspect columns for %s.%s", schema_name, table_name
        )
        return

    existing = {c.column_name: c for c in (tbl.columns or [])}

    for col_info in cols_info:
        col_name = col_info["name"]
        col_type = str(col_info.get("type", ""))

        if col_name in existing:
            existing[col_name].type = col_type
            existing[col_name].is_dttm = _is_dttm_type(col_type)
        else:
            tc = TableColumn(
                column_name=col_name,
                type=col_type,
                is_dttm=_is_dttm_type(col_type),
                groupby=True,
                filterable=True,
                table_id=tbl.id,
            )
            tbl.columns.append(tc)

    # Add 'count' metric if not present (same as original fetch_metadata)
    metric_names = {m.metric_name for m in (tbl.metrics or [])}
    if "count" not in metric_names:
        tbl.metrics.append(
            SqlMetric(
                metric_name="count",
                expression="COUNT(*)",
                verbose_name="COUNT(*)",
                table_id=tbl.id,
            )
        )


def _is_dttm_type(type_str: str) -> bool:
    """Check if a column type string represents a datetime type."""
    t = type_str.upper()
    if "INTERVAL" in t:
        return False
    return any(kw in t for kw in ("DATE", "TIME", "TIMESTAMP"))
