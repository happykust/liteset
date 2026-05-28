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
"""Report Schedule command classes — business logic for report CRUD."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.report_exceptions import (
    ChartNotFoundValidationError,
    ChartNotSavedValidationError,
    DashboardNotFoundValidationError,
    DashboardNotSavedValidationError,
    DatabaseNotFoundValidationError,
    ReportScheduleAlertRequiredDatabaseValidationError,
    ReportScheduleCreationMethodUniquenessValidationError,
    ReportScheduleEitherChartOrDashboardError,
    ReportScheduleForbiddenError,
    ReportScheduleFrequencyNotAllowed,
    ReportScheduleInvalidError,
    ReportScheduleNameUniquenessValidationError,
    ReportScheduleOnlyChartOrDashboardError,
    ReportScheduleValidationError,
)
from superset.commands.utils import compute_owner_list, populate_owner_list
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetSecurityException,
)
from superset.utils import json

try:
    from croniter import croniter
except ImportError:
    croniter = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from superset.db.daos.report import AsyncReportScheduleDAO
    from superset.models.reports import ReportSchedule


async def _validate_chart_dashboard(
    dao: "AsyncReportScheduleDAO",
    data: dict[str, Any],
    exceptions: list[ReportScheduleValidationError],
    *,
    update: bool = False,
) -> None:
    """Validate chart or dashboard relation.

    1:1 port of
    ``superset_old/commands/report/base.py::validate_chart_dashboard``.
    Resolves the referenced chart / dashboard, collecting per-field errors,
    and stores the resolved objects back into ``data`` under ``chart`` /
    ``dashboard`` (matching the original which mutates ``self._properties``).
    """
    from superset.db.daos.chart import AsyncChartDAO
    from superset.db.daos.dashboard import AsyncDashboardDAO
    from superset.models.reports import ReportCreationMethod

    chart_id = data.get("chart")
    dashboard_id = data.get("dashboard")
    creation_method = data.get("creation_method")

    if creation_method == ReportCreationMethod.CHARTS.value and not chart_id:
        # User has not saved chart yet in Explore view
        exceptions.append(ChartNotSavedValidationError())
        return

    if creation_method == ReportCreationMethod.DASHBOARDS.value and not dashboard_id:
        exceptions.append(DashboardNotSavedValidationError())
        return

    if chart_id and dashboard_id:
        exceptions.append(ReportScheduleOnlyChartOrDashboardError())

    if chart_id:
        chart = await AsyncChartDAO(dao.session).find_by_id(chart_id)
        if not chart:
            exceptions.append(ChartNotFoundValidationError())
        data["chart"] = chart
    elif dashboard_id:
        dashboard = await AsyncDashboardDAO(dao.session).find_by_id(dashboard_id)
        if not dashboard:
            exceptions.append(DashboardNotFoundValidationError())
        data["dashboard"] = dashboard
    elif not update:
        exceptions.append(ReportScheduleEitherChartOrDashboardError())


def _validate_report_frequency(
    cron_schedule: str,
    report_type: str,
    exceptions: list[ReportScheduleValidationError],
) -> None:
    """Validate the scheduled frequency against the configured minimum.

    1:1 port of
    ``superset_old/commands/report/base.py::validate_report_frequency``.
    The minimum interval is read from ``ALERT_MINIMUM_INTERVAL`` (alerts) or
    ``REPORT_MINIMUM_INTERVAL`` (reports) in the Superset settings.
    """
    from superset.models.reports import ReportScheduleType

    settings = _get_settings()
    config_key = (
        "ALERT_MINIMUM_INTERVAL"
        if report_type == ReportScheduleType.ALERT.value
        else "REPORT_MINIMUM_INTERVAL"
    )
    minimum_interval = _settings_config_get(settings, config_key, 0)
    if callable(minimum_interval):
        minimum_interval = minimum_interval()

    if not isinstance(minimum_interval, int):
        logger.error(
            "Invalid value for %s: %s", config_key, minimum_interval, exc_info=True
        )
        return

    # Since configuration is in minutes, we only need to validate
    # in case `minimum_interval` is <= 120 (2min)
    if minimum_interval < 120:
        return

    if croniter is None:
        return

    iterations = 60 if minimum_interval <= 3660 else 24
    schedule = croniter(cron_schedule)
    current_exec = next(schedule)

    for _ in range(iterations):
        next_exec = next(schedule)
        diff, current_exec = next_exec - current_exec, next_exec
        if int(diff) < minimum_interval:
            exceptions.append(
                ReportScheduleFrequencyNotAllowed(
                    report_type=report_type, minimum_interval=minimum_interval
                )
            )
            return


async def _validate_report_extra(
    dao: "AsyncReportScheduleDAO",
    data: dict[str, Any],
    exceptions: list[ReportScheduleValidationError],
) -> None:
    """Validate that the tab ids referenced in ``extra.dashboard`` exist.

    1:1 port of
    ``superset_old/commands/report/create.py::_validate_report_extra``.
    Runs only when both ``extra`` and a resolved ``dashboard`` are present.
    """
    extra = data.get("extra")
    dashboard = data.get("dashboard")

    if not extra or dashboard is None:
        return

    dashboard_state = extra.get("dashboard")
    if not dashboard_state:
        return

    position_data = json.loads(getattr(dashboard, "position_json", None) or "{}")
    active_tabs = dashboard_state.get("activeTabs") or []
    invalid_tab_ids = set(active_tabs) - set(position_data.keys())

    if anchor := dashboard_state.get("anchor"):
        try:
            anchor_list: list[str] = json.loads(anchor)
            if _invalid_tab_ids := set(anchor_list) - set(position_data.keys()):
                invalid_tab_ids.update(_invalid_tab_ids)
        except json.JSONDecodeError:
            if anchor not in position_data:
                invalid_tab_ids.add(anchor)

    if invalid_tab_ids:
        exceptions.append(
            ReportScheduleValidationError(
                f"Invalid tab ids: {invalid_tab_ids!s}",
                field_name="extra",
            )
        )


def _resolve_fk(value: Any) -> int | None:
    """Return the FK id for a value that may be an int id or an ORM object.

    ``validate`` replaces the incoming chart/dashboard/database id with the
    resolved ORM object (1:1 with the original which mutates
    ``self._properties``); ``run`` needs the integer id to write the FK column.
    """
    if value is None:
        return None
    return getattr(value, "id", value)


def _get_settings() -> Any:
    """Load SupersetSettings lazily to avoid circular imports."""
    from superset.config import SupersetSettings

    return SupersetSettings()  # type: ignore[call-arg]


def _settings_config_get(settings: Any, key: str, default: Any) -> Any:
    """Read an upstream-style config key from SupersetSettings.

    Looks in the ``feature_flags`` mapping first (where some config keys live
    in the new settings model), then falls back to a snake-cased attribute,
    then to ``default``.
    """
    feature_flags = getattr(settings, "feature_flags", None)
    if isinstance(feature_flags, dict) and key in feature_flags:
        return feature_flags[key]
    attr = getattr(settings, key.lower(), None)
    if attr is not None:
        return attr
    return default


class CreateReportScheduleCommand(AsyncBaseCommand["ReportSchedule"]):
    def __init__(
        self,
        dao: AsyncReportScheduleDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager

    async def validate(self) -> None:  # noqa: C901
        """Validate report schedule properties.

        1:1 port of ``superset_old/commands/report/create.py::validate``:
        uniqueness, alert-database existence, chart/dashboard relations,
        frequency, extra tab-ids, and per-resource creation-method uniqueness.
        Per-field errors are collected into :class:`ReportScheduleInvalidError`.
        """
        from superset.db.daos.database import AsyncDatabaseDAO
        from superset.models.reports import ReportCreationMethod, ReportScheduleType

        name = self._data.get("name")
        if not name or not str(name).strip():
            raise CommandInvalidError("name is required")

        report_type = self._data.get("type")
        if not report_type:
            raise CommandInvalidError("type is required")

        crontab = self._data.get("crontab", "")

        # Reject syntactically invalid crontab expressions up front. Upstream
        # validates this in the Marshmallow schema (``validate_crontab`` →
        # ``croniter.is_valid``); the msgspec schema doesn't, so guard here.
        if crontab and croniter is not None and not croniter.is_valid(crontab):
            raise CommandInvalidError(f"Invalid crontab: {crontab}")

        chart_id = self._data.get("chart")
        creation_method = self._data.get("creation_method")
        dashboard_id = self._data.get("dashboard")

        exceptions: list[ReportScheduleValidationError] = []

        # Validate name + type uniqueness
        if not await self._dao.validate_update_uniqueness(
            name=name, report_type=report_type
        ):
            exceptions.append(
                ReportScheduleNameUniquenessValidationError(
                    report_type=report_type, name=name
                )
            )

        # Validate if DB exists (for alerts)
        if report_type == ReportScheduleType.ALERT.value:
            database_id = self._data.get("database")
            if database_id is None:
                exceptions.append(
                    ReportScheduleAlertRequiredDatabaseValidationError()
                )
            elif database := await AsyncDatabaseDAO(self._dao.session).find_by_id(
                database_id
            ):
                self._data["database"] = database
            else:
                exceptions.append(DatabaseNotFoundValidationError())

        # Validate report frequency
        if crontab:
            _validate_report_frequency(crontab, report_type, exceptions)

        # Validate chart or dashboard relations + extra tab ids
        await _validate_chart_dashboard(self._dao, self._data, exceptions)
        await _validate_report_extra(self._dao, self._data, exceptions)

        # Each chart/dashboard may only have one report per creation method.
        if (
            creation_method != ReportCreationMethod.ALERTS_REPORTS.value
            and not await self._dao.validate_unique_creation_method(
                dashboard_id=dashboard_id,
                chart_id=chart_id,
                user_id=self._user_id,
            )
        ):
            raise ReportScheduleCreationMethodUniquenessValidationError()

        # ``validator_config_json`` arrives as a dict from the POST schema; the
        # model column stores a JSON string. Serialize before persistence
        # (1:1 with create.py:120-123).
        if self._data.get("validator_config_json") is not None:
            self._data["validator_config_json"] = json.dumps(
                self._data["validator_config_json"]
            )

        if exceptions:
            raise ReportScheduleInvalidError(exceptions=exceptions)

    async def run(self) -> "ReportSchedule":
        # Map schema fields to model fields. ``validate`` may have replaced the
        # chart/dashboard/database ids with resolved ORM objects; normalise both.
        create_data = {**self._data}
        if "chart" in create_data:
            create_data["chart_id"] = _resolve_fk(create_data.pop("chart"))
        if "dashboard" in create_data:
            create_data["dashboard_id"] = _resolve_fk(create_data.pop("dashboard"))
        if "database" in create_data:
            create_data["database_id"] = _resolve_fk(create_data.pop("database"))

        # Resolve owners separately so we can assign the M2M after insert.
        # Mirrors ``superset_old/commands/report/create.py`` which calls
        # ``populate_owners(owner_ids)`` (default_to_user=True) so the current
        # user becomes the owner when none are supplied.
        owner_ids = create_data.pop("owners", None)

        if self._user_id is not None:
            create_data["created_by_fk"] = self._user_id
            create_data["changed_by_fk"] = self._user_id

        report = await self._dao.create(create_data)

        if self._security_manager is not None:
            owners = await populate_owner_list(
                self._security_manager,
                self._user_id,
                owner_ids,
                default_to_user=True,
            )
            report.owners = owners

        await self._dao.session.flush()
        return report


class UpdateReportScheduleCommand(AsyncBaseCommand["ReportSchedule"]):
    def __init__(
        self,
        dao: AsyncReportScheduleDAO,
        pk: int,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._report: Any | None = None

    async def validate(self) -> None:  # noqa: C901
        """Validate report schedule update.

        1:1 port of ``superset_old/commands/report/update.py::validate``:
        WORKING→NOOP on deactivate, name/type uniqueness, alert-database
        existence, frequency, chart/dashboard relations, validator_config_json
        serialization, and the ownership guard.
        """
        from superset.db.daos.database import AsyncDatabaseDAO
        from superset.models.reports import ReportScheduleType, ReportState

        self._report = await self._dao.find_by_id(self._pk)
        if not self._report:
            raise ObjectNotFoundError("ReportSchedule", self._pk)

        crontab = self._data.get("crontab", self._report.crontab)
        name = self._data.get("name", self._report.name)
        report_type = self._data.get("type", self._report.type)
        database_id = self._data.get("database")

        # Reject syntactically invalid crontab expressions up front (the
        # msgspec schema doesn't, unlike upstream's Marshmallow schema).
        if (
            self._data.get("crontab") is not None
            and croniter is not None
            and not croniter.is_valid(crontab)
        ):
            raise CommandInvalidError(f"Invalid crontab: {crontab}")

        exceptions: list[ReportScheduleValidationError] = []

        # Change the state to not triggered when the user deactivates a report
        # that is currently in a working state. This prevents an alert/report
        # from being kept in a working state if activated back. 1:1 with
        # ``superset_old/commands/report/update.py:83-88``.
        if (
            self._report.last_state == ReportState.WORKING
            and "active" in self._data
            and not self._data["active"]
        ):
            self._data["last_state"] = ReportState.NOOP

        # Validate name/type uniqueness if either is changing
        if name != self._report.name or report_type != self._report.type:
            if not await self._dao.validate_update_uniqueness(
                name=name, report_type=report_type, report_id=self._pk
            ):
                exceptions.append(
                    ReportScheduleNameUniquenessValidationError(
                        report_type=report_type, name=name
                    )
                )

        # Validate if DB exists (for alerts). 1:1 with update.py:102-105 which
        # assigns the resolved (possibly ``None``) database back to properties.
        if report_type == ReportScheduleType.ALERT.value and database_id:
            database = await AsyncDatabaseDAO(self._dao.session).find_by_id(
                database_id
            )
            if not database:
                exceptions.append(DatabaseNotFoundValidationError())
            self._data["database"] = database

        # Validate report frequency
        if crontab:
            _validate_report_frequency(crontab, report_type, exceptions)

        # Validate chart or dashboard relations
        await _validate_chart_dashboard(
            self._dao, self._data, exceptions, update=True
        )

        # ``validator_config_json`` arrives as a dict from the PUT schema; the
        # model column stores a JSON string (1:1 with update.py:119-122).
        if self._data.get("validator_config_json") is not None:
            self._data["validator_config_json"] = json.dumps(
                self._data["validator_config_json"]
            )

        # Check ownership — non-owners (and non-admins) get a 403, 1:1 with
        # ``superset_old/commands/report/update.py:124-128``.
        if self._security_manager is not None:
            try:
                await self._security_manager.raise_for_ownership(
                    self._report, self._user_id
                )
            except SupersetSecurityException as ex:
                raise ReportScheduleForbiddenError() from ex

        if exceptions:
            raise ReportScheduleInvalidError(exceptions=exceptions)

    async def run(self) -> "ReportSchedule":
        assert self._report is not None

        update_data = {**self._data}
        if "chart" in update_data:
            update_data["chart_id"] = _resolve_fk(update_data.pop("chart"))
        if "dashboard" in update_data:
            update_data["dashboard_id"] = _resolve_fk(update_data.pop("dashboard"))
        if "database" in update_data:
            update_data["database_id"] = _resolve_fk(update_data.pop("database"))

        # Resolve owners separately. Mirrors
        # ``superset_old/commands/report/update.py`` which calls
        # ``compute_owners(model.owners, owner_ids)`` — preserving the existing
        # owners when none are supplied in the payload.
        owners_in_payload = "owners" in update_data
        owner_ids = update_data.pop("owners", None)

        if self._user_id is not None:
            update_data["changed_by_fk"] = self._user_id

        report = await self._dao.update(self._report, update_data)

        if self._security_manager is not None:
            await self._dao.session.refresh(report, ["owners"])
            owners = await compute_owner_list(
                self._security_manager,
                self._user_id,
                list(report.owners),
                owner_ids if owners_in_payload else None,
            )
            report.owners = owners

        await self._dao.session.flush()
        return report


class DeleteReportScheduleCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncReportScheduleDAO,
        pk: int,
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._user_id = user_id
        self._security_manager = security_manager
        self._report: Any | None = None

    async def validate(self) -> None:
        self._report = await self._dao.find_by_id(self._pk)
        if not self._report:
            raise ObjectNotFoundError("ReportSchedule", self._pk)

        # Check ownership — 1:1 with
        # ``superset_old/commands/report/delete.py:53-58``.
        if self._security_manager is not None:
            try:
                await self._security_manager.raise_for_ownership(
                    self._report, self._user_id
                )
            except SupersetSecurityException as ex:
                raise ReportScheduleForbiddenError() from ex

    async def run(self) -> None:
        assert self._report is not None
        await self._dao.delete([self._report])
        await self._dao.session.flush()


class BulkDeleteReportScheduleCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncReportScheduleDAO,
        ids: list[int],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._ids = ids
        self._user_id = user_id
        self._security_manager = security_manager
        self._reports: list[Any] = []

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No report schedule IDs provided")
        self._reports = await self._dao.find_by_ids(self._ids)
        found_ids = {int(r.id) for r in self._reports}
        missing = set(self._ids) - found_ids
        if missing:
            raise ObjectNotFoundError("ReportSchedule", str(sorted(missing)))

        # Check ownership for every report — 1:1 with
        # ``superset_old/commands/report/delete.py:53-58``.
        if self._security_manager is not None:
            for model in self._reports:
                try:
                    await self._security_manager.raise_for_ownership(
                        model, self._user_id
                    )
                except SupersetSecurityException as ex:
                    raise ReportScheduleForbiddenError() from ex

    async def run(self) -> None:
        await self._dao.delete(self._reports)
        await self._dao.session.flush()
