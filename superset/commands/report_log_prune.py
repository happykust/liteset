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
"""Prune execution logs across all report schedules.

Direct port of
``superset_old/commands/report/log_prune.py::AsyncPruneReportScheduleLogCommand``.

For every :class:`~superset.models.reports.ReportSchedule` that defines a
``log_retention`` window, deletes :class:`~superset.models.reports.ReportExecutionLog`
rows whose ``end_dttm`` is older than that window.  Schedules with a ``NULL``
``log_retention`` are skipped entirely (their logs are kept indefinitely),
matching the original behaviour exactly.

The command runs synchronously against a regular SQLAlchemy ``Session``
because it is invoked from the ``reports.prune_log`` Celery beat task.  The
original wrapped ``run`` in ``@transaction()`` — i.e. the whole prune is
atomic: if any schedule errors the entire operation is rolled back and a
:class:`ReportSchedulePruneLogError` is raised.  That semantics is preserved.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from superset.commands.report_exceptions import ReportSchedulePruneLogError
from superset.models.reports import ReportExecutionLog, ReportSchedule

logger = logging.getLogger(__name__)


# pylint: disable=consider-using-transaction
class PruneReportScheduleLogCommand:
    """Prune execution logs for all report schedules."""

    def run(self, session: Any | None = None) -> None:
        """Execute the prune command.

        Args:
            session: Optional synchronous :class:`sqlalchemy.orm.Session`.
                When omitted a fresh session is taken from
                :func:`superset.db.session.get_sync_session`.
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
        self.validate()
        prune_errors = []

        for report_schedule in session.query(ReportSchedule).all():
            if report_schedule.log_retention is not None:
                from_date = datetime.utcnow() - timedelta(
                    days=report_schedule.log_retention
                )
                try:
                    row_count = (
                        session.query(ReportExecutionLog)
                        .filter(
                            ReportExecutionLog.report_schedule_id == report_schedule.id,
                            ReportExecutionLog.end_dttm < from_date,
                        )
                        .delete(synchronize_session="fetch")
                    )
                    logger.info(
                        "Deleted %s logs for report schedule id: %s",
                        str(row_count),
                        str(report_schedule.id),
                    )
                except SQLAlchemyError as ex:
                    prune_errors.append(str(ex))
        if prune_errors:
            session.rollback()
            raise ReportSchedulePruneLogError(";".join(prune_errors))
        session.commit()

    def validate(self) -> None:
        """No-op validation — kept for API compatibility with the original."""


__all__ = ["PruneReportScheduleLogCommand"]
