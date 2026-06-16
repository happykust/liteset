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
"""Flask-free port of ``tests/integration_tests/reports/alert_tests.py``.

Drives the real synchronous ``AlertCommand``
(``superset.commands.report_alert``) against the seeded Postgres backend.

Differences from the upstream integration test, all behaviour-preserving:

* Config is no longer read from ``app.config``.  ``AlertCommand`` resolves
  deployment knobs through ``superset.commands.report_alert._get_settings``
  (a freshly constructed :class:`~superset.config.SupersetSettings`), so the
  per-test ``ALERT_REPORTS_EXECUTORS`` / ``MUTATE_ALERT_QUERY`` /
  ``alert_reports_query_execution_max_tries`` overrides are injected by
  patching ``_get_settings`` to return a settings instance with those fields
  overridden via ``model_copy``.
* The executor user is resolved by ``get_executor`` + ``AlertCommand._find_user``
  which queries the **synchronous** Celery ``Session``; the command therefore
  receives the real sync session (``get_sync_session``) and the executor users
  (``gamma`` / ``alpha`` / ``admin``) are inserted on it.
* ``override_user`` is invoked inside ``AlertCommand._execute_query`` (imported
  from ``superset.utils.core``), so the patch target is
  ``superset.utils.core.override_user`` rather than the command module symbol.
* ``Database.get_df`` is patched so the assertion exercises only the executor
  resolution / mutate / retry logic without depending on the example engine's
  live ``SELECT``.
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext, suppress
from typing import Any

import pandas as pd
import pytest
from pytest_mock import MockerFixture
from sqlalchemy.orm import Session

from superset.commands.report_exceptions import AlertQueryError
from superset.config import SupersetSettings
from superset.db.session import get_sync_session
from superset.models.reports import (
    ReportCreationMethod,
    ReportSchedule,
    ReportScheduleType,
)
from superset.models.security import User
from superset.tasks.types import ExecutorType, FixedExecutor
from superset.utils.database import get_example_database


@pytest.fixture
def sync_session() -> Session:
    """Synchronous Celery-style session, rolled back after each test.

    ``AlertCommand`` runs synchronously inside a Celery worker, so it needs a
    plain ``sqlalchemy.orm.Session`` (not the async ``db_session``).  Rows are
    rolled back at teardown to keep the seeded backend pristine.
    """
    session = get_sync_session()
    try:
        yield session
    finally:
        session.rollback()


@pytest.fixture
def get_user(sync_session: Session):
    """Return a factory that gets-or-creates a real ``User`` by username."""

    def _get_user(username: str) -> User:
        user = sync_session.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(
                username=username,
                first_name=username,
                last_name="user",
                email=f"{username}@example.com",
            )
            sync_session.add(user)
            sync_session.flush()
        return user

    return _get_user


def _settings_with(**overrides: Any) -> SupersetSettings:
    """Build a real settings instance with selected fields overridden."""
    return SupersetSettings().model_copy(update=overrides)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "owner_names,creator_name,config,expected_result",
    [
        (["gamma"], None, [FixedExecutor("admin")], "admin"),
        (["gamma"], None, [ExecutorType.OWNER], "gamma"),
        (
            ["alpha", "gamma"],
            "gamma",
            [ExecutorType.CREATOR_OWNER],
            "gamma",
        ),
        (
            ["alpha", "gamma"],
            "alpha",
            [ExecutorType.CREATOR_OWNER],
            "alpha",
        ),
        (
            ["alpha", "gamma"],
            "admin",
            [ExecutorType.CREATOR_OWNER],
            AlertQueryError(),
        ),
        (["gamma"], None, [ExecutorType.CURRENT_USER], AlertQueryError()),
    ],
)
def test_execute_query_as_report_executor(
    owner_names: list[str],
    creator_name: str | None,
    config: list[Any],
    expected_result: Any,
    mocker: MockerFixture,
    sync_session: Session,
    get_user,
) -> None:
    # ``admin`` is part of the seeded fixture user set upstream; ensure it
    # exists so ``FixedExecutor("admin")`` resolves to a real user.
    get_user("admin")
    owners = [get_user(owner_name) for owner_name in owner_names]
    report_schedule = ReportSchedule(
        created_by=get_user(creator_name) if creator_name else None,
        owners=owners,
        type=ReportScheduleType.ALERT,
        description="description",
        crontab="0 9 * * *",
        creation_method=ReportCreationMethod.ALERTS_REPORTS,
        sql="SELECT 1",
        grace_period=14400,
        working_timeout=3600,
        database=get_example_database(),
        validator_config_json='{"op": "==", "threshold": 1}',
    )
    from superset.commands.report_alert import AlertCommand

    command = AlertCommand(
        report_schedule=report_schedule,
        execution_id=uuid.uuid4(),
        session=sync_session,
    )
    settings = _settings_with(
        alert_reports_executors=config,
        alert_reports_query_execution_max_tries=1,
        mutate_alert_query=False,
    )
    mocker.patch("superset.commands.report_alert._get_settings", return_value=settings)
    mocker.patch.object(
        report_schedule.database,
        "get_df",
        return_value=pd.DataFrame([{"sample_col": 1}]),
    )
    override_user_mock = mocker.patch("superset.utils.core.override_user")
    cm = (
        pytest.raises(type(expected_result))
        if isinstance(expected_result, Exception)
        else nullcontext()
    )
    with cm:
        command.run()
        assert override_user_mock.call_args[0][0].username == expected_result


def test_execute_query_mutate_query_enabled(
    mocker: MockerFixture,
    sync_session: Session,
    get_user,
) -> None:
    settings = _settings_with(
        mutate_alert_query=True,
        alert_reports_executors=[ExecutorType.OWNER],
        alert_reports_query_execution_max_tries=1,
    )
    mocker.patch("superset.commands.report_alert._get_settings", return_value=settings)
    mocker.patch("superset.utils.core.override_user")
    mock_df = mocker.MagicMock(spec=pd.DataFrame)
    mock_df.empty = True
    mock_database = get_example_database()
    mock_get_df = mocker.patch.object(mock_database, "get_df", return_value=mock_df)
    mock_limited_sql = mocker.patch.object(mock_database, "apply_limit_to_sql")
    mock_mutate_call = mocker.patch.object(mock_database, "mutate_sql_based_on_config")

    report_schedule = ReportSchedule(
        created_by=get_user("admin"),
        owners=[get_user("admin")],
        type=ReportScheduleType.ALERT,
        description="description",
        crontab="0 9 * * *",
        creation_method=ReportCreationMethod.ALERTS_REPORTS,
        sql="SELECT 1",
        grace_period=14400,
        working_timeout=3600,
        database=mock_database,
        validator_config_json='{"op": "==", "threshold": 1}',
    )
    from superset.commands.report_alert import AlertCommand

    AlertCommand(
        report_schedule=report_schedule,
        execution_id=uuid.uuid4(),
        session=sync_session,
    ).run()

    mock_mutate_call.assert_called_once_with(mock_limited_sql.return_value)
    mock_get_df.assert_called_once_with(sql=mock_mutate_call.return_value)


def test_execute_query_mutate_query_disabled(
    mocker: MockerFixture,
    sync_session: Session,
    get_user,
) -> None:
    settings = _settings_with(
        mutate_alert_query=False,
        alert_reports_executors=[ExecutorType.OWNER],
        alert_reports_query_execution_max_tries=1,
    )
    mocker.patch("superset.commands.report_alert._get_settings", return_value=settings)
    mocker.patch("superset.utils.core.override_user")
    mock_database = mocker.MagicMock()

    report_schedule = ReportSchedule(
        created_by=get_user("admin"),
        owners=[get_user("admin")],
        type=ReportScheduleType.ALERT,
        description="description",
        crontab="0 9 * * *",
        creation_method=ReportCreationMethod.ALERTS_REPORTS,
        sql="SELECT 1",
        grace_period=14400,
        working_timeout=3600,
        database=mock_database,
        validator_config_json='{"op": "==", "threshold": 1}',
    )
    from superset.commands.report_alert import AlertCommand

    AlertCommand(
        report_schedule=report_schedule,
        execution_id=uuid.uuid4(),
        session=sync_session,
    ).run()

    mock_database.mutate_sql_based_on_config.assert_not_called()
    mock_database.get_df.assert_called_once_with(
        sql=mock_database.apply_limit_to_sql.return_value
    )


def test_execute_query_succeeded_no_retry(mocker: MockerFixture) -> None:
    from superset.commands.report_alert import AlertCommand

    settings = _settings_with(alert_reports_query_execution_max_tries=3)
    mocker.patch("superset.commands.report_alert._get_settings", return_value=settings)
    execute_query_mock = mocker.patch(
        "superset.commands.report_alert.AlertCommand._execute_query",
        side_effect=lambda: pd.DataFrame([{"sample_col": 0}]),
    )

    command = AlertCommand(report_schedule=mocker.Mock(), execution_id=uuid.uuid4())

    command.validate()

    assert execute_query_mock.call_count == 1


def test_execute_query_succeeded_with_retries(mocker: MockerFixture) -> None:
    from superset.commands.report_alert import AlertCommand, AlertQueryError

    settings = _settings_with(alert_reports_query_execution_max_tries=3)
    mocker.patch("superset.commands.report_alert._get_settings", return_value=settings)
    execute_query_mock = mocker.patch(
        "superset.commands.report_alert.AlertCommand._execute_query"
    )

    query_executed_count = 0
    # Should match the configured ``alert_reports_query_execution_max_tries``.
    expected_max_retries = 3

    def _mocked_execute_query() -> pd.DataFrame:
        nonlocal query_executed_count
        query_executed_count += 1

        if query_executed_count < expected_max_retries:
            raise AlertQueryError()
        else:
            return pd.DataFrame([{"sample_col": 0}])

    execute_query_mock.side_effect = _mocked_execute_query
    execute_query_mock.__name__ = "mocked_execute_query"

    command = AlertCommand(report_schedule=mocker.Mock(), execution_id=uuid.uuid4())

    command.validate()

    assert execute_query_mock.call_count == expected_max_retries


def test_execute_query_failed_no_retry(mocker: MockerFixture) -> None:
    from superset.commands.report_alert import AlertCommand, AlertQueryTimeout

    settings = _settings_with(alert_reports_query_execution_max_tries=3)
    mocker.patch("superset.commands.report_alert._get_settings", return_value=settings)
    execute_query_mock = mocker.patch(
        "superset.commands.report_alert.AlertCommand._execute_query"
    )

    def _mocked_execute_query() -> None:
        raise AlertQueryTimeout

    execute_query_mock.side_effect = _mocked_execute_query
    execute_query_mock.__name__ = "mocked_execute_query"

    command = AlertCommand(report_schedule=mocker.Mock(), execution_id=uuid.uuid4())

    with suppress(AlertQueryTimeout):
        command.validate()
    assert execute_query_mock.call_count == 1


def test_execute_query_failed_max_retries(mocker: MockerFixture) -> None:
    from superset.commands.report_alert import AlertCommand, AlertQueryError

    settings = _settings_with(alert_reports_query_execution_max_tries=3)
    mocker.patch("superset.commands.report_alert._get_settings", return_value=settings)
    execute_query_mock = mocker.patch(
        "superset.commands.report_alert.AlertCommand._execute_query"
    )

    def _mocked_execute_query() -> None:
        raise AlertQueryError

    execute_query_mock.side_effect = _mocked_execute_query
    execute_query_mock.__name__ = "mocked_execute_query"

    command = AlertCommand(report_schedule=mocker.Mock(), execution_id=uuid.uuid4())

    with suppress(AlertQueryError):
        command.validate()
    # Should match the configured ``alert_reports_query_execution_max_tries``.
    assert execute_query_mock.call_count == 3


@pytest.mark.skip(
    reason="AlertCommand._get_alert_metadata_from_object / @logs_context is not "
    "ported in Liteset: the async port drops the logs-context decorator and its "
    "metadata helper, so there is no behaviour to assert here."
)
def test_get_alert_metadata_from_object() -> None:  # pragma: no cover
    pass
