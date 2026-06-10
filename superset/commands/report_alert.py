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
"""Alert condition evaluation command.

Ported 1:1 from ``superset_old/commands/report/alert.py``.
Runs **synchronously** inside a Celery worker.  Uses a plain
:class:`~sqlalchemy.orm.Session` (not async) obtained from
:func:`superset.db.session.get_sync_session` or passed directly.

The command:
1. Renders the alert SQL template via Jinja.
2. Executes it against the report's associated database.
3. Validates the result against the configured validator (NOT_NULL / OPERATOR).
4. Returns ``True`` if the alert condition is triggered.
"""

from __future__ import annotations

import logging
from operator import eq, ge, gt, le, lt, ne
from timeit import default_timer
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from superset.commands.report_exceptions import (
    AlertQueryError,
    AlertQueryInvalidTypeError,
    AlertQueryMultipleColumnsError,
    AlertQueryMultipleRowsError,
    AlertQueryTimeout,
    AlertValidatorConfigError,
)
from superset.models.core import Database
from superset.models.reports import ReportSchedule, ReportScheduleValidatorType
from superset.utils import json
from superset.utils.retries import retry_call

logger = logging.getLogger(__name__)

ALERT_SQL_LIMIT = 2

# All sql statements have an applied LIMIT,
# to avoid heavy loads done by a user mistake
OPERATOR_FUNCTIONS: dict[str, Any] = {
    ">=": ge,
    ">": gt,
    "<=": le,
    "<": lt,
    "==": eq,
    "!=": ne,
}


def _get_settings() -> Any:
    """Load SupersetSettings lazily to avoid circular imports."""
    from superset.config import SupersetSettings

    return SupersetSettings()  # type: ignore[call-arg]


class AlertCommand:
    """Evaluate an alert SQL query and check whether it triggers.

    Ported 1:1 from ``:AlertCommand`` in
    ``superset_old/commands/report/alert.py``.
    """

    def __init__(
        self,
        report_schedule: ReportSchedule,
        execution_id: UUID,
        session: Session | None = None,
    ) -> None:
        self._report_schedule = report_schedule
        self._execution_id = execution_id
        self._session = session
        self._result: float | None = None

    def run(self) -> bool:
        """Execute the alert SQL query and validate the result.

        Sets ``report_schedule.last_value`` or ``last_value_row_json``
        with the query result.

        :return: ``True`` if the alert condition is triggered.
        :raises AlertQueryError: SQL query is not valid.
        :raises AlertQueryInvalidTypeError: Output is not an allowed type.
        :raises AlertQueryMultipleColumnsError: Query returned multiple columns.
        :raises AlertQueryMultipleRowsError: Query returned multiple rows.
        :raises AlertQueryTimeout: Celery soft timeout exceeded.
        :raises AlertValidatorConfigError: Validator config is not valid.
        """
        self.validate()

        if self._is_validator_not_null:
            self._report_schedule.last_value_row_json = str(self._result)
            return self._result not in (0, None, np.nan)
        self._report_schedule.last_value = self._result
        try:
            operator = json.loads(self._report_schedule.validator_config_json)["op"]
            threshold = json.loads(self._report_schedule.validator_config_json)[
                "threshold"
            ]
            return OPERATOR_FUNCTIONS[operator](self._result, threshold)  # type: ignore[arg-type]
        except (KeyError, json.JSONDecodeError) as ex:
            raise AlertValidatorConfigError() from ex

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_not_null(self, rows: np.recarray[Any, Any]) -> None:
        self._validate_result(rows)
        self._result = rows[0][1]

    @staticmethod
    def _validate_result(rows: np.recarray[Any, Any]) -> None:
        # check if query returned more than one row
        if len(rows) > 1:
            raise AlertQueryMultipleRowsError(
                message=(
                    f"Alert query returned more than one row. {len(rows)} rows returned"
                ),
            )
        # check if query returned more than one column
        if len(rows[0]) > 2:
            raise AlertQueryMultipleColumnsError(
                # len is subtracted by 1 to discard pandas index column
                message=(
                    f"Alert query returned more than one column. "
                    f"{len(rows[0]) - 1} columns returned"
                ),
            )

    def _validate_operator(self, rows: np.recarray[Any, Any]) -> None:
        self._validate_result(rows)
        if rows[0][1] in (0, None, np.nan):
            self._result = 0.0
            return
        try:
            # Check if it's float or if we can convert it
            self._result = float(rows[0][1])
            return
        except (AssertionError, TypeError, ValueError) as ex:
            raise AlertQueryInvalidTypeError() from ex

    @property
    def _is_validator_not_null(self) -> bool:
        return (
            self._report_schedule.validator_type == ReportScheduleValidatorType.NOT_NULL
        )

    @property
    def _is_validator_operator(self) -> bool:
        return (
            self._report_schedule.validator_type == ReportScheduleValidatorType.OPERATOR
        )

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def _find_user(self, username: str | None) -> Any | None:
        """Resolve the executor user by username via the sync session.

        Mirrors ``ExecuteReportScheduleCommand._find_user``; returns
        ``None`` when no session/username is available, in which case
        ``override_user(None)`` simply leaves the user context unset
        (matching the original's tolerance of a missing user).
        """
        if not username or self._session is None:
            return None
        from superset.models.security import User

        return self._session.query(User).filter(User.username == username).one_or_none()

    def _execute_query(self) -> pd.DataFrame:
        """Execute the actual alert SQL query template.

        1:1 with ``superset_old/commands/report/alert.py::_execute_query``:
        renders the SQL through the **sandboxed** Jinja processor
        (``superset.jinja_context``), applies the engine-spec-aware
        ``Database.apply_limit_to_sql`` (LIMIT 2), optionally mutates it
        (``MUTATE_ALERT_QUERY`` → ``mutate_sql_based_on_config``), then runs
        it through ``Database.get_df`` under the resolved executor user
        (``override_user``) so RLS and Jinja ``current_user`` apply.

        :return: A pandas DataFrame with query results.
        :raises AlertQueryError: SQL query is not valid.
        :raises AlertQueryTimeout: Celery soft timeout exceeded.
        """
        from superset.jinja_context import get_template_processor
        from superset.tasks.utils import get_executor
        from superset.utils.core import override_user

        database: Database | None = self._report_schedule.database
        if database is None:
            raise AlertQueryError(message="Alert has no associated database")

        settings = _get_settings()

        sql_template = get_template_processor(database=database)
        rendered_sql = sql_template.process_template(self._report_schedule.sql)
        try:
            limited_rendered_sql = database.apply_limit_to_sql(
                rendered_sql, ALERT_SQL_LIMIT
            )

            if settings.mutate_alert_query:
                limited_rendered_sql = database.mutate_sql_based_on_config(
                    limited_rendered_sql
                )

            _executor, username = get_executor(
                executors=settings.alert_reports_executors,
                model=self._report_schedule,
            )
            user = self._find_user(username)
            with override_user(user):
                start = default_timer()
                df = database.get_df(sql=limited_rendered_sql)
                stop = default_timer()
                logger.info(
                    "Query for %s took %.2f ms",
                    self._execution_id,
                    (stop - start) * 1000.0,
                )
                return df
        except SoftTimeLimitExceeded as ex:
            logger.warning("A timeout occurred while executing the alert query: %s", ex)
            raise AlertQueryTimeout() from ex
        except Exception as ex:
            logger.warning("An error occurred when running alert query")
            # The exception message here can reveal too much information to
            # malicious users, so we raise a generic message.
            raise AlertQueryError(
                message="An error occurred when running alert query"
            ) from ex

    def validate(self) -> None:
        """Validate the query result as a Pandas DataFrame."""
        settings = _get_settings()

        # When there are transient errors when executing queries, users will get
        # notified with the error stacktrace which can be avoided by retrying
        df = retry_call(
            self._execute_query,
            exception=AlertQueryError,
            max_tries=settings.alert_reports_query_execution_max_tries,
        )

        if df.empty and self._is_validator_not_null:
            self._result = None
            return
        if df.empty and self._is_validator_operator:
            self._result = 0.0
            return
        rows = df.to_records()
        if self._is_validator_not_null:
            self._validate_not_null(rows)
            return
        self._validate_operator(rows)
