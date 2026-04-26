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
from sqlalchemy import text as sa_text
from sqlalchemy.engine import create_engine as _create_engine
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

    def _execute_query(self) -> pd.DataFrame:
        """Execute the alert SQL query against the report's database.

        In the original implementation this uses Jinja template processing
        and the Database model's ``get_df``/``apply_limit_to_sql`` methods.
        Since our Database model is pure SQLAlchemy without Flask helpers,
        we execute the SQL directly via a sync engine created from the
        database's connection URI.

        :return: A pandas DataFrame with query results.
        :raises AlertQueryError: SQL query is not valid.
        :raises AlertQueryTimeout: Celery soft timeout exceeded.
        """
        sql = self._report_schedule.sql
        if not sql:
            raise AlertQueryError(message="Alert SQL query is empty")

        database: Database | None = self._report_schedule.database
        if database is None:
            raise AlertQueryError(message="Alert has no associated database")

        settings = _get_settings()

        # Apply SQL LIMIT to prevent heavy loads from user mistakes.
        # Simple approach: wrap in a subquery with LIMIT.
        limited_sql = (
            f"SELECT * FROM ({sql.rstrip().rstrip(';')})"  # noqa: S608
            f" AS __alert_sq LIMIT {ALERT_SQL_LIMIT}"
        )

        # If the config has a SQL query mutator and MUTATE_ALERT_QUERY is True,
        # apply it. In our settings-based config this is a callable or None.
        if settings.mutate_alert_query and settings.sql_query_mutator:
            try:
                limited_sql = settings.sql_query_mutator(
                    limited_sql,
                    security_manager=None,
                    database=database,
                )
            except Exception:
                logger.warning(
                    "SQL query mutator failed for alert %s, using unmutated SQL",
                    self._execution_id,
                )

        try:
            start = default_timer()

            # Create a disposable engine from the database's decrypted URI
            engine = _create_engine(database.sqlalchemy_uri_decrypted)
            try:
                df = pd.read_sql_query(sa_text(limited_sql), engine)
            finally:
                engine.dispose()

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
