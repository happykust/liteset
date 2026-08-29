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
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.commands.report import (
    BulkDeleteReportScheduleCommand,
    CreateReportScheduleCommand,
    DeleteReportScheduleCommand,
    UpdateReportScheduleCommand,
)
from superset.exceptions import CommandInvalidError, ObjectNotFoundError


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
    # ``_validate_chart_dashboard``/``_find_accessible_database`` now resolve
    # the referenced chart/dashboard/database through an access-filtered
    # ``AsyncChartDAO``/``AsyncDashboardDAO``/``AsyncDatabaseDAO`` query
    # (``session.execute(...).scalars().unique().all()``) instead of a bare
    # ``session.get(...)``. A plain ``AsyncMock()`` session auto-generates
    # ``.execute()``'s return value as ANOTHER AsyncMock, so the chained sync
    # ``.scalars()``/``.unique()``/``.all()`` calls blow up. Configure it to
    # return a stand-in row so these tests (which are about the OTHER
    # validation rules, not chart/dashboard/database access itself — see
    # test_report_schedule_access_scoping.py for that) keep resolving the
    # referenced chart/dashboard/database as "found", like before.
    execute_result = MagicMock()
    execute_result.scalars.return_value.unique.return_value.all.return_value = [
        MagicMock(id=1)
    ]
    dao.session.execute = AsyncMock(return_value=execute_result)
    return dao


@pytest.fixture
def mock_report():
    report = MagicMock()
    report.id = 1
    report.name = "Test Report"
    report.type = "Report"
    report.crontab = "0 * * * *"
    report.timezone = "UTC"
    report.active = True
    report.description = ""
    report.chart_id = None
    report.dashboard_id = None
    report.database_id = None
    return report


@pytest.fixture
def mock_security_manager():
    sm = MagicMock()
    current_user = MagicMock()
    current_user.id = 1
    sm.find_user_by_id = AsyncMock(
        side_effect=lambda uid: current_user if uid == 1 else None
    )
    sm.is_admin = MagicMock(return_value=True)
    sm.raise_for_ownership = AsyncMock()
    return sm


@pytest.fixture(autouse=True)
def patch_build_async_security_manager(mock_security_manager):
    with patch(
        "superset.security.manager.build_async_security_manager",
        return_value=mock_security_manager,
    ):
        yield


async def test_create_report_validates_name_required(mock_dao):
    cmd = CreateReportScheduleCommand(dao=mock_dao, data={"type": "Report"}, user_id=1)
    with pytest.raises(CommandInvalidError, match="name is required"):
        await cmd.validate()


async def test_create_report_validates_empty_name(mock_dao):
    cmd = CreateReportScheduleCommand(
        dao=mock_dao, data={"name": "  ", "type": "Report"}, user_id=1
    )
    with pytest.raises(CommandInvalidError, match="name is required"):
        await cmd.validate()


async def test_create_report_validates_type_required(mock_dao):
    cmd = CreateReportScheduleCommand(dao=mock_dao, data={"name": "Test"}, user_id=1)
    with pytest.raises(CommandInvalidError, match="type is required"):
        await cmd.validate()


async def test_create_report_validates_crontab_invalid(mock_dao):
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={"name": "Test", "type": "Report", "crontab": "not a cron"},
        user_id=1,
    )
    # croniter may not be installed in test env; skip if not available
    try:
        from croniter import croniter  # noqa: F401
    except ImportError:
        pytest.skip("croniter not installed")
    with pytest.raises(CommandInvalidError, match="Invalid crontab"):
        await cmd.validate()


async def test_create_report_validates_uniqueness(mock_dao):
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=False)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={"name": "Existing", "type": "Report", "crontab": "0 * * * *"},
        user_id=1,
    )
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await cmd.validate()
    assert "already exists" in str(exc_info.value.normalized_messages())


async def test_create_report_success(mock_dao, mock_report):
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.create = AsyncMock(return_value=mock_report)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
            "chart": 1,
        },
        user_id=1,
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.id == 1
    assert result.name == "Test Report"
    mock_dao.create.assert_awaited_once()


async def test_create_report_rejects_database_reference(mock_dao):
    """A REPORT-type payload carrying a database must be rejected with a
    field-keyed error:
    ``{"database": ["Database reference is not allowed on a report"]}``.
    """
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.validate_unique_creation_method = AsyncMock(return_value=True)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
            "chart": 1,
            "database": 5,
        },
        user_id=1,
    )
    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert messages["database"] == ["Database reference is not allowed on a report"]


async def test_create_report_without_database_allowed(mock_dao):
    """A REPORT-type payload with no database reference is fine.

    The schema omits an absent ``database`` from the loaded dict (UNSET →
    filter_unset), so the command sees NO key. The rule is key-presence
    based (``"database" in data``).
    """
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.validate_unique_creation_method = AsyncMock(return_value=True)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
            "chart": 1,
        },
        user_id=1,
    )
    # Must not raise on the database-reference rule (chart is set so the
    # chart/dashboard rule passes too).
    await cmd.validate()


async def test_create_report_with_database_key_rejected(mock_dao):
    """A REPORT-type payload CONTAINING a database key is rejected.

    The rule checks key presence (``"database" in data``), not value truthiness.
    """
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.validate_unique_creation_method = AsyncMock(return_value=True)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
            "chart": 1,
            "database": 7,
        },
        user_id=1,
    )
    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await cmd.validate()
    assert "database" in exc_info.value.normalized_messages()


async def test_create_report_rejects_custom_width_below_min(mock_dao):
    """custom_width below ALERT_REPORTS_MIN_CUSTOM_SCREENSHOT_WIDTH → 422."""
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.validate_unique_creation_method = AsyncMock(return_value=True)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
            "chart": 1,
            "custom_width": 10,  # below default min=600
        },
        user_id=1,
    )
    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert "custom_width" in messages
    assert "600px" in messages["custom_width"][0]


async def test_create_report_rejects_custom_width_above_max(mock_dao):
    """custom_width above ALERT_REPORTS_MAX_CUSTOM_SCREENSHOT_WIDTH → 422."""
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.validate_unique_creation_method = AsyncMock(return_value=True)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
            "chart": 1,
            "custom_width": 9999,  # above default max=2400
        },
        user_id=1,
    )
    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert "custom_width" in messages
    assert "2400px" in messages["custom_width"][0]


async def test_create_report_allows_valid_custom_width(mock_dao):
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.validate_unique_creation_method = AsyncMock(return_value=True)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
            "chart": 1,
            "custom_width": 1200,  # within [600, 2400]
        },
        user_id=1,
    )
    # Should not raise a custom_width error (chart validation handled separately)
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    try:
        await cmd.validate()
    except ReportScheduleInvalidError as exc:
        messages = exc.normalized_messages()
        assert "custom_width" not in messages


async def test_create_report_run_strips_none_report_format(mock_dao, mock_report):
    """report_format=None must NOT be written; DB column default ("PNG") must apply.

    When the field is absent from the request body, the deserialized dict omits
    it so the DB column default fires.  The msgspec schema default is "PNG"
    (fixes the None-override), but the command also strips it to guard against
    direct command usage.
    """
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.validate_unique_creation_method = AsyncMock(return_value=True)
    mock_dao.create = AsyncMock(return_value=mock_report)

    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
            "chart": 1,
            "report_format": None,  # simulate old schema behaviour
        },
        user_id=1,
    )
    await cmd.validate()
    await cmd.run()

    call_kwargs = mock_dao.create.call_args
    passed_data: dict = (
        call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("data", {})
    )
    assert (
        passed_data.get("report_format") is not None
        or "report_format" not in passed_data
    )


async def test_update_report_rejects_custom_width_out_of_range(mock_dao, mock_report):
    """custom_width validation runs on PUT as well."""
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    mock_dao.find_by_id = AsyncMock(return_value=mock_report)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    cmd = UpdateReportScheduleCommand(
        dao=mock_dao,
        pk=1,
        data={"custom_width": 50},  # below min=600
    )
    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert "custom_width" in messages
    assert "600px" in messages["custom_width"][0]


async def test_update_report_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateReportScheduleCommand(dao=mock_dao, pk=999, data={"name": "X"})
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_report_validates_uniqueness(mock_dao, mock_report):
    mock_dao.find_by_id = AsyncMock(return_value=mock_report)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=False)
    cmd = UpdateReportScheduleCommand(
        dao=mock_dao, pk=1, data={"name": "Duplicate Name"}
    )
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await cmd.validate()
    assert "already exists" in str(exc_info.value.normalized_messages())


async def test_update_report_success(mock_dao, mock_report):
    mock_dao.find_by_id = AsyncMock(return_value=mock_report)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.update = AsyncMock(return_value=mock_report)
    cmd = UpdateReportScheduleCommand(dao=mock_dao, pk=1, data={"name": "Updated Name"})
    await cmd.validate()
    result = await cmd.run()
    assert result.id == 1
    mock_dao.update.assert_awaited_once()
    mock_dao.session.flush.assert_awaited_once()


async def test_delete_report_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteReportScheduleCommand(dao=mock_dao, pk=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_report_success(mock_dao, mock_report):
    mock_dao.find_by_id = AsyncMock(return_value=mock_report)
    mock_dao.delete = AsyncMock()
    cmd = DeleteReportScheduleCommand(dao=mock_dao, pk=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_report])
    mock_dao.session.flush.assert_awaited_once()


async def test_bulk_delete_empty_ids(mock_dao):
    cmd = BulkDeleteReportScheduleCommand(dao=mock_dao, ids=[])
    with pytest.raises(CommandInvalidError, match="No report schedule IDs"):
        await cmd.validate()


async def test_bulk_delete_missing_ids(mock_dao, mock_report):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_report])
    cmd = BulkDeleteReportScheduleCommand(dao=mock_dao, ids=[1, 2, 3])
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_bulk_delete_success(mock_dao, mock_report):
    report2 = MagicMock()
    report2.id = 2
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_report, report2])
    mock_dao.delete = AsyncMock()
    cmd = BulkDeleteReportScheduleCommand(dao=mock_dao, ids=[1, 2])
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_report, report2])
    mock_dao.session.flush.assert_awaited_once()


async def test_bulk_delete_db_error_raises_delete_failed_error(mock_dao, mock_report):
    """A SQLAlchemy error during bulk-delete must surface as
    ReportScheduleDeleteFailedError, not a raw SQLAlchemyError.

    ``BulkDeleteReportScheduleCommand.run()`` must wrap DB failures in
    ``ReportScheduleDeleteFailedError`` so the API handler returns 422
    instead of 500.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from superset.commands.report_exceptions import ReportScheduleDeleteFailedError

    mock_dao.find_by_ids = AsyncMock(return_value=[mock_report])
    mock_dao.delete = AsyncMock(side_effect=SQLAlchemyError("connection lost"))
    cmd = BulkDeleteReportScheduleCommand(dao=mock_dao, ids=[1])
    await cmd.validate()
    with pytest.raises(ReportScheduleDeleteFailedError):
        await cmd.run()


async def test_create_report_invalid_owner_produces_field_keyed_error(mock_dao):
    """Invalid owner IDs on POST must raise ReportScheduleInvalidError with
    normalized_messages() == {"owners": ["Owners are invalid"]}.

    Owner errors are collected into ReportScheduleInvalidError (field-keyed
    dict message) rather than escaping as OwnersNotFoundValidationError.
    """
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.validate_unique_creation_method = AsyncMock(return_value=True)

    current_user = MagicMock()
    current_user.id = 1

    sm = MagicMock()
    sm.find_user_by_id = AsyncMock(
        side_effect=lambda uid: current_user if uid == 1 else None
    )
    sm.is_admin = MagicMock(return_value=True)

    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
            "chart": 1,
            "owners": [999],  # invalid owner id
        },
        user_id=1,
        security_manager=sm,
    )
    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert messages == {"owners": ["Owners are invalid"]}


async def test_update_report_invalid_owner_produces_field_keyed_error(
    mock_dao, mock_report
):
    """Invalid owner IDs on PUT must raise ReportScheduleInvalidError with
    normalized_messages() == {"owners": ["Owners are invalid"]}.

    Owner errors are collected into ReportScheduleInvalidError (field-keyed
    dict message) rather than escaping as OwnersNotFoundValidationError.
    """
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    mock_dao.find_by_id = AsyncMock(return_value=mock_report)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_report.owners = []

    current_user = MagicMock()
    current_user.id = 1

    sm = MagicMock()
    sm.find_user_by_id = AsyncMock(
        side_effect=lambda uid: current_user if uid == 1 else None
    )
    sm.is_admin = MagicMock(return_value=True)
    sm.raise_for_ownership = AsyncMock()

    cmd = UpdateReportScheduleCommand(
        dao=mock_dao,
        pk=1,
        data={"owners": [999]},  # invalid owner id
        user_id=1,
        security_manager=sm,
    )
    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert messages == {"owners": ["Owners are invalid"]}


# Regression: asyncio.get_event_loop() + loop.run_until_complete() / asyncio.run()
# both raise RuntimeError when called from a sync function running on the async
# event loop thread. Use cache_manager.sync_cache (synchronous Redis/null/memory
# backend) instead.


def _make_sync_cache_mock(get_return: object = None) -> MagicMock:
    m = MagicMock()
    m.get = MagicMock(return_value=get_return)
    m.set = MagicMock(return_value=None)
    return m


def _make_cm_mock(sync_cache_mock: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.sync_cache = sync_cache_mock
    return cm


def test_slack_cache_get_returns_list_from_sync_cache():
    from superset.controllers.report import _slack_cache_get

    channels = [{"id": "C1", "name": "general"}]
    sync_cache = _make_sync_cache_mock(get_return=channels)
    cm = _make_cm_mock(sync_cache)

    with patch("superset.extensions.cache_manager", cm):
        result = _slack_cache_get()

    assert result == channels
    sync_cache.get.assert_called_once_with("slack_conversations_list")


def test_slack_cache_get_returns_none_on_cache_miss():
    from superset.controllers.report import _slack_cache_get

    sync_cache = _make_sync_cache_mock(get_return=None)
    cm = _make_cm_mock(sync_cache)

    with patch("superset.extensions.cache_manager", cm):
        result = _slack_cache_get()

    assert result is None


def test_slack_cache_get_handles_json_string_payload():
    """Legacy JSON-string payload (bytes written before schema migration) is decoded."""
    from superset.controllers.report import _slack_cache_get

    channels = [{"id": "C2", "name": "random"}]
    sync_cache = _make_sync_cache_mock(get_return=json.dumps(channels))
    cm = _make_cm_mock(sync_cache)

    with patch("superset.extensions.cache_manager", cm):
        result = _slack_cache_get()

    assert result == channels


def test_slack_cache_get_handles_bytes_payload():
    """Legacy bytes payload is decoded and JSON-parsed."""
    from superset.controllers.report import _slack_cache_get

    channels = [{"id": "C3", "name": "ops"}]
    sync_cache = _make_sync_cache_mock(get_return=json.dumps(channels).encode())
    cm = _make_cm_mock(sync_cache)

    with patch("superset.extensions.cache_manager", cm):
        result = _slack_cache_get()

    assert result == channels


def test_slack_cache_get_swallows_exception():
    from superset.controllers.report import _slack_cache_get

    sync_cache = MagicMock()
    sync_cache.get = MagicMock(side_effect=RuntimeError("redis down"))
    cm = _make_cm_mock(sync_cache)

    with patch("superset.extensions.cache_manager", cm):
        result = _slack_cache_get()

    assert result is None


def test_slack_cache_get_does_not_use_asyncio_run():
    """_slack_cache_get must never call asyncio.run() or loop.run_until_complete().

    This is the core regression guard: the old broken code tried to run async
    cache ops from a sync function on the event loop thread, causing RuntimeError
    that silently disabled caching.  The fix uses sync_cache so asyncio is not
    involved at all.
    """
    import asyncio

    from superset.controllers.report import _slack_cache_get

    channels = [{"id": "C4", "name": "alerts"}]
    sync_cache = _make_sync_cache_mock(get_return=channels)
    cm = _make_cm_mock(sync_cache)

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "_slack_cache_get must not call asyncio.run() — "
            "it runs on the event-loop thread"
        )

    with patch("superset.extensions.cache_manager", cm):
        with patch.object(asyncio, "run", side_effect=_fail_if_called):
            result = _slack_cache_get()

    assert result == channels


def test_slack_cache_set_calls_sync_cache_set():
    from superset.controllers.report import _slack_cache_set

    channels = [{"id": "C5", "name": "devops"}]
    ttl = 1800
    sync_cache = _make_sync_cache_mock()
    cm = _make_cm_mock(sync_cache)

    with patch("superset.extensions.cache_manager", cm):
        _slack_cache_set(channels, ttl)

    sync_cache.set.assert_called_once_with(
        "slack_conversations_list", channels, ttl=ttl
    )


def test_slack_cache_set_swallows_exception():
    from superset.controllers.report import _slack_cache_set

    sync_cache = MagicMock()
    sync_cache.set = MagicMock(side_effect=RuntimeError("redis down"))
    cm = _make_cm_mock(sync_cache)

    with patch("superset.extensions.cache_manager", cm):
        _slack_cache_set([{"id": "C6", "name": "test"}], 600)


def test_slack_cache_set_does_not_use_asyncio_run():
    """_slack_cache_set must never call asyncio.run()."""
    import asyncio

    from superset.controllers.report import _slack_cache_set

    sync_cache = _make_sync_cache_mock()
    cm = _make_cm_mock(sync_cache)

    def _fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "_slack_cache_set must not call asyncio.run() — "
            "it runs on the event-loop thread"
        )

    with patch("superset.extensions.cache_manager", cm):
        with patch.object(asyncio, "run", side_effect=_fail_if_called):
            _slack_cache_set([{"id": "C7", "name": "infra"}], 900)

    sync_cache.set.assert_called_once()


def test_get_slack_channels_uses_cache_hit_and_skips_api():
    from superset.controllers.report import _get_slack_channels

    cached = [{"id": "C8", "name": "cached-channel", "is_private": False}]
    sync_cache = _make_sync_cache_mock(get_return=cached)
    cm = _make_cm_mock(sync_cache)

    with (
        patch("superset.extensions.cache_manager", cm),
        patch("superset.controllers.report._slack_fetch_all_channels") as mock_fetch,
        patch("slack_sdk.WebClient", MagicMock()),
        patch(
            "superset.config.SupersetSettings",
            return_value=MagicMock(
                slack_api_token="xoxb-test",
                slack_proxy=None,
                slack_api_rate_limit_retry_count=2,
                slack_cache_timeout=1800,
            ),
        ),
    ):
        result = _get_slack_channels()

    mock_fetch.assert_not_called()
    assert result == cached


def test_get_slack_channels_stores_result_in_cache_on_miss():
    from superset.controllers.report import _get_slack_channels

    fetched = [{"id": "C9", "name": "fresh", "is_private": False}]
    sync_cache = _make_sync_cache_mock(get_return=None)
    cm = _make_cm_mock(sync_cache)

    mock_settings = MagicMock()
    mock_settings.slack_api_token = "xoxb-test"
    mock_settings.slack_proxy = None
    mock_settings.slack_api_rate_limit_retry_count = 2
    mock_settings.slack_cache_timeout = 1800

    with (
        patch("superset.extensions.cache_manager", cm),
        patch(
            "superset.controllers.report._slack_fetch_all_channels",
            return_value=fetched,
        ),
        patch("slack_sdk.WebClient", MagicMock()),
        patch(
            "superset.config.SupersetSettings",
            return_value=mock_settings,
        ),
    ):
        result = _get_slack_channels()

    assert result == fetched
    sync_cache.set.assert_called_once_with(
        "slack_conversations_list", fetched, ttl=1800
    )


def test_slack_channels_requires_can_write() -> None:
    """GET /report/slack_channels/ must require ``can_write ReportSchedule``.
    A ``can_read`` gate would let view-only users enumerate the workspace's
    Slack channels (and trigger Slack API calls)."""
    from superset.controllers.report import ReportScheduleController

    handler = ReportScheduleController.slack_channels
    perm_tuples = [
        c
        for g in (handler.guards or [])
        for c in (cell.cell_contents for cell in (g.__closure__ or []))
        if isinstance(c, tuple) and len(c) == 2
    ]
    assert ("can_write", "ReportSchedule") in perm_tuples, perm_tuples


def test_report_schedule_invalid_error_uses_field_keyed_handler():
    """ReportScheduleInvalidError must be routed to the per-field 422 handler so
    the response carries {field: [messages]} instead of a flat string message."""
    from superset.app import _build_exception_handlers
    from superset.commands.dataset.exceptions import dataset_invalid_error_handler
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    handlers = _build_exception_handlers()
    assert handlers.get(ReportScheduleInvalidError) is dataset_invalid_error_handler


def test_report_schedule_invalid_error_normalized_messages_is_field_keyed():
    """normalized_messages() builds the {field_name: [messages]} mapping the
    front-end consumes."""
    from superset.commands.report_exceptions import (
        ReportScheduleInvalidError,
        ReportScheduleValidationError,
    )

    exc = ReportScheduleInvalidError(
        exceptions=[
            ReportScheduleValidationError("Owners are invalid", field_name="owners")
        ]
    )
    assert exc.status_code == 422
    assert exc.normalized_messages() == {"owners": ["Owners are invalid"]}
