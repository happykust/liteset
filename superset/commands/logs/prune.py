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
"""Prune old rows from the FAB ``logs`` activity table.

The command deletes records from the :class:`~superset.models.core.Log`
table that are older than ``retention_period_days`` days.  Used by the
``prune_logs`` Celery beat task to keep the activity log table from
growing without bound.

The command runs synchronously against a regular SQLAlchemy ``Session``
because it is invoked from a Celery worker — async machinery is not
required and would only complicate the worker setup.  Deletes are
performed in batches of 999 rows so that SQLite (which caps the size of
an ``IN`` clause at 999) can be used as the metadata DB without
modification.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

from superset.models.core import Log

logger = logging.getLogger(__name__)


# pylint: disable=consider-using-transaction
class LogPruneCommand:
    """Prune the ``logs`` table by deleting rows older than the retention period.

    Attributes:
        retention_period_days: The number of days for which records should
            be retained. Records older than this period will be deleted.
    """

    def __init__(self, retention_period_days: int) -> None:
        """Construct the command.

        Args:
            retention_period_days: Number of days to keep in the logs table.
        """
        self.retention_period_days = retention_period_days

    def run(self, session: Any | None = None) -> None:
        """Execute the prune command.

        Args:
            session: Optional synchronous :class:`sqlalchemy.orm.Session` to
                use for the deletes.  When omitted a fresh session is taken
                from :func:`superset.db.session.get_sync_session` (the path
                used by the Celery task wrapper).
        """
        if session is None:
            from superset.db.session import get_sync_session

            session = get_sync_session()
            owns_session = True
        else:
            owns_session = False

        try:
            self._run_with_session(session)
        finally:
            if owns_session:
                session.close()

    def _run_with_session(self, session: Any) -> None:
        batch_size = 999  # SQLite caps IN clauses at 999 items
        total_deleted = 0
        start_time = time.time()

        ids_to_delete = (
            session.execute(
                sa.select(Log.id).where(
                    Log.dttm
                    < datetime.now() - timedelta(days=self.retention_period_days)
                )
            )
            .scalars()
            .all()
        )

        total_rows = len(ids_to_delete)

        logger.info("Total rows to be deleted: %s", f"{total_rows:,}")

        next_logging_threshold = 1

        for i in range(0, total_rows, batch_size):
            batch_ids = ids_to_delete[i : i + batch_size]
            result = session.execute(sa.delete(Log).where(Log.id.in_(batch_ids)))
            total_deleted += result.rowcount
            # Commit each batch so progress survives a later batch failure.
            session.commit()
            percentage_complete = (
                (total_deleted / total_rows) * 100 if total_rows else 100
            )
            if percentage_complete >= next_logging_threshold:
                logger.info(
                    "Deleted %s rows from the logs table older than %s days "
                    "(%d%% complete)",
                    f"{total_deleted:,}",
                    self.retention_period_days,
                    percentage_complete,
                )
                next_logging_threshold += 1

        elapsed_time = time.time() - start_time
        minutes, seconds = divmod(elapsed_time, 60)
        formatted_time = f"{int(minutes):02}:{int(seconds):02}"
        logger.info(
            "Pruning complete: %s rows deleted in %s",
            f"{total_deleted:,}",
            formatted_time,
        )

    def validate(self) -> None:
        """No-op validation."""


__all__ = ["LogPruneCommand"]
