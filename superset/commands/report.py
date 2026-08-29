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
    ReportScheduleCreateFailedError,
    ReportScheduleCreationMethodUniquenessValidationError,
    ReportScheduleDeleteFailedError,
    ReportScheduleEitherChartOrDashboardError,
    ReportScheduleForbiddenError,
    ReportScheduleFrequencyNotAllowed,
    ReportScheduleInvalidError,
    ReportScheduleNameUniquenessValidationError,
    ReportScheduleOnlyChartOrDashboardError,
    ReportScheduleUpdateFailedError,
    ReportScheduleValidationError,
)
from superset.commands.utils import compute_owner_list, populate_owner_list
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    OwnersNotFoundValidationError,
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


async def _resolve_security_manager_and_user(
    dao: "AsyncReportScheduleDAO",
    security_manager: Any | None,
    user_id: int | None,
) -> tuple[Any | None, Any | None]:
    """Resolve ``(security_manager, user)`` once for the access-filtered
    chart/dashboard/database lookups below.

    Builds a manager (bound to ``dao.session``) when the caller didn't supply
    one, mirroring the fallback already used for owners further down in
    ``validate()``. When no concrete acting user can be resolved (``user_id``
    is ``None`` — CLI/system contexts with no security_manager threaded
    either), the filter callers degrade to unfiltered lookups, consistent
    with every other "no security context" fallback in this codebase
    (``AsyncExportModelsCommand._validate_access``, ``filter_visible_ids``,
    the async import helpers in ``chart/importers/v1/utils.py``, ...).
    """
    sm = security_manager
    if sm is None:
        from superset.security.manager import build_async_security_manager

        sm = build_async_security_manager(dao.session, _get_settings())

    user = None
    if user_id is not None:
        user = await sm.find_user_by_id(user_id)

    return sm, user


async def _find_accessible_database(
    session: Any,
    database_id: int,
    security_manager: Any | None,
    user: Any | None,
) -> Any | None:
    """Resolve an ALERT's referenced database, scoped to what ``user`` can see.

    ``AsyncDatabaseDAO.find_by_id`` carries no base filter (unlike upstream's
    DAO), so without this an Alpha with no grant on the target connection
    could still bind — and run — an alert against it. Mirrors
    ``_validate_chart_dashboard``'s access-filtered lookup below.
    """
    from superset.db.daos.database import AsyncDatabaseDAO

    if security_manager is None or user is None:
        return await AsyncDatabaseDAO(session).find_by_id(database_id)

    from superset.db.filters import database_access_filters
    from superset.models.core import Database

    access_filters = await database_access_filters(security_manager, user)
    results = await AsyncDatabaseDAO(session).find_all(
        filters=[Database.id == database_id, *access_filters],
        page=0,
        page_size=1,
    )
    return results[0] if results else None


async def _validate_chart_dashboard(  # noqa: C901
    dao: "AsyncReportScheduleDAO",
    data: dict[str, Any],
    exceptions: list[ReportScheduleValidationError],
    security_manager: Any | None = None,
    user: Any | None = None,
    *,
    update: bool = False,
) -> None:
    """Validate chart or dashboard relation.

    Resolves the referenced chart / dashboard, collecting per-field errors,
    and stores the resolved objects back into ``data`` under ``chart`` /
    ``dashboard``.

    Resolution IS the authorization here: neither the alert-execution path
    (``report_alert.py``) nor the dashboard-render path re-checks access at
    run time, so an id the caller cannot see must resolve to "not found" —
    exactly like ``GET``/``PUT`` on the chart/dashboard endpoints themselves.
    ``security_manager``/``user`` are optional so CLI/test callers that don't
    thread a security context keep the previous unfiltered behaviour.
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
        chart = None
        if security_manager is not None and user is not None:
            from superset.db.filters import chart_access_filters
            from superset.models.slice import Slice

            access_filters = await chart_access_filters(security_manager, user)
            results = await AsyncChartDAO(dao.session).find_all(
                filters=[Slice.id == chart_id, *access_filters],
                page=0,
                page_size=1,
            )
            chart = results[0] if results else None
        else:
            chart = await AsyncChartDAO(dao.session).find_by_id(chart_id)
        if not chart:
            exceptions.append(ChartNotFoundValidationError())
        data["chart"] = chart
    elif dashboard_id:
        dashboard = None
        if security_manager is not None and user is not None:
            from superset.db.filters import dashboard_access_filters
            from superset.models.dashboard import Dashboard

            access_filters = await dashboard_access_filters(security_manager, user)
            dashboard_results = await AsyncDashboardDAO(dao.session).find_all(
                filters=[Dashboard.id == dashboard_id, *access_filters],
                page=0,
                page_size=1,
            )
            dashboard = dashboard_results[0] if dashboard_results else None
        else:
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
    resolved ORM object; ``run`` needs the integer id to write the FK column.
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
    if (attr := getattr(settings, key.lower(), None)) is not None:
        return attr
    return default


async def _raise_not_found_or_forbidden(
    security_manager: Any,
    user_id: int | None,
    pk: Any,
    ex: Exception,
) -> None:
    """Map an ownership denial to 404-vs-403.

    ``ReportScheduleDAO.find_by_id(s)`` applies ``ReportScheduleFilter``
    (owners-scope unless the user has ``can_access_all_datasources``), so a
    report the user can't even see is a ``ReportScheduleNotFoundError`` (404)
    *before* the ownership check; the 403 from ``raise_for_ownership`` is
    reachable only for users who can see every report.  The DAO has no base
    filter, so reproduce the same outcome here: visible-but-not-owner → 403,
    invisible → 404.
    """
    user = None
    if user_id is not None:
        user = await security_manager.find_user_by_id(user_id)
    can_access_all = user is not None and (
        await security_manager.can_access_all_datasources(user=user)
    )
    if not can_access_all:
        raise ObjectNotFoundError("ReportSchedule", pk) from ex
    raise ReportScheduleForbiddenError() from ex


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
        self._owners: list[Any] | None = None

    async def validate(self) -> None:  # noqa: C901
        from superset.models.reports import ReportCreationMethod, ReportScheduleType

        name = self._data.get("name")
        if not name or not str(name).strip():
            raise CommandInvalidError("name is required")

        report_type = self._data.get("type")
        if not report_type:
            raise CommandInvalidError("type is required")

        crontab = self._data.get("crontab", "")

        # Upstream validates crontab in Marshmallow schema; msgspec schema
        # doesn't, so guard here.
        if crontab and croniter is not None and not croniter.is_valid(crontab):
            raise CommandInvalidError(f"Invalid crontab: {crontab}")

        chart_id = self._data.get("chart")
        creation_method = self._data.get("creation_method")
        dashboard_id = self._data.get("dashboard")

        exceptions: list[ReportScheduleValidationError] = []

        # Resolved once and reused below for the database/chart/dashboard
        # lookups so an id the caller cannot see resolves to "not found"
        # rather than silently binding the schedule to an inaccessible
        # object (neither the alert-execution nor the dashboard-render path
        # re-checks access at run time — this resolution IS the check).
        sm, sm_user = await _resolve_security_manager_and_user(
            self._dao, self._security_manager, self._user_id
        )

        if not await self._dao.validate_update_uniqueness(
            name=name, report_type=report_type
        ):
            exceptions.append(
                ReportScheduleNameUniquenessValidationError(
                    report_type=report_type, name=name
                )
            )

        # A REPORT (as opposed to an ALERT) must not carry a database reference.
        # Checks KEY presence (``"database" in data``), not value truthiness —
        # an explicit ``"database": null`` on a REPORT is also rejected.
        if report_type == ReportScheduleType.REPORT.value and "database" in self._data:
            exceptions.append(
                ReportScheduleValidationError(
                    "Database reference is not allowed on a report",
                    field_name="database",
                )
            )

        if report_type == ReportScheduleType.ALERT.value:
            database_id = self._data.get("database")
            if database_id is None:
                exceptions.append(ReportScheduleAlertRequiredDatabaseValidationError())
            else:
                database = await _find_accessible_database(
                    self._dao.session, database_id, sm, sm_user
                )
                if database is not None:
                    self._data["database"] = database
                else:
                    exceptions.append(DatabaseNotFoundValidationError())

        if crontab:
            _validate_report_frequency(crontab, report_type, exceptions)

        await _validate_chart_dashboard(self._dao, self._data, exceptions, sm, sm_user)
        await _validate_report_extra(self._dao, self._data, exceptions)

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
        # model column stores a JSON string. Serialize before persistence.
        if self._data.get("validator_config_json") is not None:
            self._data["validator_config_json"] = json.dumps(
                self._data["validator_config_json"]
            )

        custom_width = self._data.get("custom_width")
        if custom_width is not None:
            settings = _get_settings()
            min_width = _settings_config_get(
                settings, "ALERT_REPORTS_MIN_CUSTOM_SCREENSHOT_WIDTH", 600
            )
            max_width = _settings_config_get(
                settings, "ALERT_REPORTS_MAX_CUSTOM_SCREENSHOT_WIDTH", 2400
            )
            if not (min_width <= custom_width <= max_width):
                exceptions.append(
                    ReportScheduleValidationError(
                        f"Screenshot width must be between {min_width}px"
                        f" and {max_width}px",
                        field_name="custom_width",
                    )
                )

        # ``sm`` was already resolved above for the database/chart/dashboard
        # access checks — reuse it rather than building a second manager.
        owner_ids_raw: list[int] | None = self._data.get("owners")
        try:
            self._owners = await populate_owner_list(
                sm,
                self._user_id,
                owner_ids_raw,
                default_to_user=True,
            )
        except OwnersNotFoundValidationError:
            exceptions.append(
                ReportScheduleValidationError("Owners are invalid", field_name="owners")
            )

        if exceptions:
            raise ReportScheduleInvalidError(exceptions=exceptions)

    async def run(self) -> "ReportSchedule":
        create_data = {**self._data}

        # ``report_format`` must not be written as ``None`` — the DB column has
        # ``default=ReportDataFormat.VISUALIZATION.value`` ("PNG") which only
        # applies when the column is omitted entirely.  The POST schema now
        # defaults to "PNG", but guard against any None that slips through
        # (e.g., direct command usage without the schema).
        if create_data.get("report_format") is None:
            create_data.pop("report_format", None)

        if "chart" in create_data:
            create_data["chart_id"] = _resolve_fk(create_data.pop("chart"))
        if "dashboard" in create_data:
            create_data["dashboard_id"] = _resolve_fk(create_data.pop("dashboard"))
        if "database" in create_data:
            create_data["database_id"] = _resolve_fk(create_data.pop("database"))

        # Owners were resolved (and validated) in validate(); use the cached
        # result rather than calling populate_owner_list again.  The list is
        # baked into ``create_data`` BEFORE the DAO creates the model so the
        # DAO's ``setattr(item, "owners", [...])`` runs on a TRANSIENT instance
        # (no session attached → no lazy-load attempt).  This prevents the
        # MissingGreenlet crash described in [[sa-lazy-load-on-transient-asyncpg]].
        create_data.pop("owners", None)

        if self._user_id is not None:
            create_data["created_by_fk"] = self._user_id
            create_data["changed_by_fk"] = self._user_id

        if self._owners is not None:
            create_data["owners"] = self._owners

        from sqlalchemy.exc import SQLAlchemyError

        try:
            report = await self._dao.create(create_data)
            await self._dao.session.flush()
        except SQLAlchemyError as ex:
            raise ReportScheduleCreateFailedError() from ex
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
        self._owners: list[Any] | None = None

    async def validate(self) -> None:  # noqa: C901
        from superset.models.reports import ReportState

        self._report = await self._dao.find_by_id(self._pk)
        if not self._report:
            raise ObjectNotFoundError("ReportSchedule", self._pk)

        crontab = self._data.get("crontab", self._report.crontab)
        name = self._data.get("name", self._report.name)
        report_type = self._data.get("type", self._report.type)
        database_id = self._data.get("database")

        # msgspec schema doesn't validate crontab unlike upstream's Marshmallow schema.
        if (
            self._data.get("crontab") is not None
            and croniter is not None
            and not croniter.is_valid(crontab)
        ):
            raise CommandInvalidError(f"Invalid crontab: {crontab}")

        exceptions: list[ReportScheduleValidationError] = []

        # Resolved once and reused below for the database/chart/dashboard
        # lookups (access-scoped, so an id the caller cannot see resolves to
        # "not found") and for the ownership check further down.
        sm, sm_user = await _resolve_security_manager_and_user(
            self._dao, self._security_manager, self._user_id
        )

        # Change the state to not triggered when the user deactivates a report
        # that is currently in a working state. This prevents an alert/report
        # from being kept in a working state if activated back.
        if (
            self._report.last_state == ReportState.WORKING
            and "active" in self._data
            and not self._data["active"]
        ):
            self._data["last_state"] = ReportState.NOOP

        if name != self._report.name or report_type != self._report.type:
            if not await self._dao.validate_update_uniqueness(
                name=name, report_type=report_type, report_id=self._pk
            ):
                exceptions.append(
                    ReportScheduleNameUniquenessValidationError(
                        report_type=report_type, name=name
                    )
                )

        # Validate the database binding whenever one is supplied, NOT only for
        # ``type == Alert``. ``run()`` persists ``database_id`` unconditionally,
        # so gating the access check on a caller-controlled field let a writer
        # bind an inaccessible database under ``type: Report`` and then flip the
        # type to ``Alert`` in a second request — at which point the alert
        # executes arbitrary SQL on a connection they were never granted.
        if database_id:
            database = await _find_accessible_database(
                self._dao.session, database_id, sm, sm_user
            )
            if not database:
                exceptions.append(DatabaseNotFoundValidationError())
            self._data["database"] = database

        if crontab:
            _validate_report_frequency(crontab, report_type, exceptions)

        await _validate_chart_dashboard(
            self._dao, self._data, exceptions, sm, sm_user, update=True
        )

        if self._data.get("validator_config_json") is not None:
            self._data["validator_config_json"] = json.dumps(
                self._data["validator_config_json"]
            )

        custom_width = self._data.get("custom_width")
        if custom_width is not None:
            settings = _get_settings()
            min_width = _settings_config_get(
                settings, "ALERT_REPORTS_MIN_CUSTOM_SCREENSHOT_WIDTH", 600
            )
            max_width = _settings_config_get(
                settings, "ALERT_REPORTS_MAX_CUSTOM_SCREENSHOT_WIDTH", 2400
            )
            if not (min_width <= custom_width <= max_width):
                exceptions.append(
                    ReportScheduleValidationError(
                        f"Screenshot width must be between {min_width}px"
                        f" and {max_width}px",
                        field_name="custom_width",
                    )
                )

        # ``sm`` was already resolved above for the database/chart/dashboard
        # access checks — reuse it rather than building a second manager.
        # ``_resolve_security_manager_and_user`` always returns a manager (it
        # builds one when none was injected), so this cannot be None here.
        assert sm is not None
        try:
            await sm.raise_for_ownership(self._report, self._user_id)
        except SupersetSecurityException as ex:
            await _raise_not_found_or_forbidden(sm, self._user_id, self._pk, ex)

        owner_ids_raw: list[int] | None = self._data.get("owners")
        # Eagerly load current owners (asyncpg cannot lazy-load relationships).
        await self._dao.session.refresh(self._report, ["owners"])
        try:
            self._owners = await compute_owner_list(
                sm,
                self._user_id,
                list(self._report.owners),
                owner_ids_raw,
            )
        except OwnersNotFoundValidationError:
            exceptions.append(
                ReportScheduleValidationError("Owners are invalid", field_name="owners")
            )

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

        update_data.pop("owners", None)

        if self._user_id is not None:
            update_data["changed_by_fk"] = self._user_id

        from sqlalchemy.exc import SQLAlchemyError

        try:
            report = await self._dao.update(self._report, update_data)

            if self._owners is not None:
                report.owners = self._owners

            await self._dao.session.flush()
        except SQLAlchemyError as ex:
            raise ReportScheduleUpdateFailedError() from ex
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

        if self._security_manager is not None:
            try:
                await self._security_manager.raise_for_ownership(
                    self._report, self._user_id
                )
            except SupersetSecurityException as ex:
                await _raise_not_found_or_forbidden(
                    self._security_manager, self._user_id, self._pk, ex
                )

    async def run(self) -> None:
        assert self._report is not None
        from sqlalchemy.exc import SQLAlchemyError

        try:
            await self._dao.delete([self._report])
            await self._dao.session.flush()
        except SQLAlchemyError as ex:
            raise ReportScheduleDeleteFailedError() from ex


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

        if self._security_manager is not None:
            for model in self._reports:
                try:
                    await self._security_manager.raise_for_ownership(
                        model, self._user_id
                    )
                except SupersetSecurityException as ex:
                    await _raise_not_found_or_forbidden(
                        self._security_manager, self._user_id, model.id, ex
                    )

    async def run(self) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        try:
            await self._dao.delete(self._reports)
            await self._dao.session.flush()
        except SQLAlchemyError as ex:
            raise ReportScheduleDeleteFailedError() from ex
