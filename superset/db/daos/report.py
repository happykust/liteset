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

from datetime import datetime
from typing import Any

from sqlalchemy import CursorResult, delete, select

from superset.db.base_dao import BaseAsyncDAO
from superset.models.reports import (
    ReportExecutionLog,
    ReportRecipients,
    ReportSchedule,
    ReportState,
)
from superset.utils.json import dumps

REPORT_SCHEDULE_ERROR_NOTIFICATION_MARKER = "Notification sent with error"


class AsyncReportScheduleDAO(BaseAsyncDAO[ReportSchedule]):
    model_cls = ReportSchedule

    async def find_by_chart_id(self, chart_id: int) -> list[ReportSchedule]:
        return await self.find_all([ReportSchedule.chart_id == chart_id])

    async def find_by_dashboard_id(
        self,
        dashboard_id: int,
    ) -> list[ReportSchedule]:
        return await self.find_all([ReportSchedule.dashboard_id == dashboard_id])

    async def find_by_database_id(
        self,
        database_id: int,
    ) -> list[ReportSchedule]:
        return await self.find_all([ReportSchedule.database_id == database_id])

    async def validate_update_uniqueness(
        self,
        name: str,
        report_type: str,
        report_id: int | None = None,
    ) -> bool:
        stmt = select(ReportSchedule.id).where(
            ReportSchedule.name == name,
            ReportSchedule.type == report_type,
        )
        if report_id is not None:
            stmt = stmt.where(ReportSchedule.id != report_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def create(self, attributes: dict[str, Any]) -> ReportSchedule:
        """Create a report schedule with nested recipients.

        Recipients are attached to the still-transient report via the
        ``report_schedule`` backref BEFORE the flush, so the
        ``cascade="all, delete-orphan"`` relationship persists them.
        (Appending to the collection AFTER a flush — on the now-persistent
        report — would fire a sync SELECT and raise MissingGreenlet; see
        [[sa-lazy-load-on-transient-asyncpg]].)
        """
        attributes = {**attributes}
        recipients_data = attributes.pop("recipients", [])
        report = await super().create(attributes)

        for recipient in recipients_data:
            config = recipient.get("recipient_config_json", "")
            if isinstance(config, dict):
                config = dumps(config)
            # ``report_schedule=report`` wires the backref on the transient
            # report; the relationship cascade persists it on flush.
            ReportRecipients(
                type=recipient["type"],
                recipient_config_json=config,
                report_schedule=report,
            )

        await self.session.flush([report])
        return report

    async def update(
        self,
        item: ReportSchedule,
        attributes: dict[str, Any],
    ) -> ReportSchedule:
        attributes = {**attributes}
        recipients_data = attributes.pop("recipients", None)

        item = await super().update(item, attributes)

        if recipients_data is not None:
            await self.session.refresh(item, ["recipients"])
            for old_rec in list(item.recipients):
                item.recipients.remove(old_rec)
                await self.session.delete(old_rec)

            for recipient in recipients_data:
                config = recipient.get("recipient_config_json", "")
                if isinstance(config, dict):
                    config = dumps(config)
                rec = ReportRecipients(
                    type=recipient["type"],
                    recipient_config_json=config,
                    report_schedule_id=item.id,
                )
                item.recipients.append(rec)

        return item

    async def find_active(self) -> list[ReportSchedule]:
        return await self.find_all([ReportSchedule.active.is_(True)])

    async def find_last_success_log(
        self,
        report_schedule: ReportSchedule,
    ) -> ReportExecutionLog | None:
        stmt = (
            select(ReportExecutionLog)
            .where(
                ReportExecutionLog.report_schedule_id == report_schedule.id,
                ReportExecutionLog.state == ReportState.SUCCESS,
            )
            .order_by(ReportExecutionLog.end_dttm.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_last_entered_working_log(
        self,
        report_schedule: ReportSchedule,
    ) -> ReportExecutionLog | None:
        stmt = (
            select(ReportExecutionLog)
            .where(
                ReportExecutionLog.report_schedule_id == report_schedule.id,
                ReportExecutionLog.state == ReportState.WORKING,
                ReportExecutionLog.error_message.is_(None),
            )
            .order_by(ReportExecutionLog.end_dttm.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_last_error_notification(
        self,
        report_schedule: ReportSchedule,
    ) -> ReportExecutionLog | None:
        last_error_stmt = (
            select(ReportExecutionLog)
            .where(
                ReportExecutionLog.report_schedule_id == report_schedule.id,
                ReportExecutionLog.error_message
                == REPORT_SCHEDULE_ERROR_NOTIFICATION_MARKER,
            )
            .order_by(ReportExecutionLog.end_dttm.desc())
            .limit(1)
        )
        result = await self.session.execute(last_error_stmt)
        last_error_log = result.scalars().one_or_none()
        if not last_error_log:
            return None

        # Check that only errors have occurred since the last notification
        non_error_stmt = (
            select(ReportExecutionLog)
            .where(
                ReportExecutionLog.report_schedule_id == report_schedule.id,
                ReportExecutionLog.state.notin_(
                    [ReportState.ERROR, ReportState.WORKING]
                ),
                ReportExecutionLog.end_dttm < last_error_log.end_dttm,
            )
            .order_by(ReportExecutionLog.end_dttm.desc())
            .limit(1)
        )
        non_error_result = await self.session.execute(non_error_stmt)
        non_error_log = non_error_result.scalars().one_or_none()

        return last_error_log if not non_error_log else None

    async def find_by_chart_ids(self, chart_ids: list[int]) -> list[ReportSchedule]:
        if not chart_ids:
            return []
        return await self.find_all([ReportSchedule.chart_id.in_(chart_ids)])

    async def find_by_dashboard_ids(
        self, dashboard_ids: list[int]
    ) -> list[ReportSchedule]:
        if not dashboard_ids:
            return []
        return await self.find_all([ReportSchedule.dashboard_id.in_(dashboard_ids)])

    async def find_by_database_ids(
        self, database_ids: list[int]
    ) -> list[ReportSchedule]:
        if not database_ids:
            return []
        return await self.find_all([ReportSchedule.database_id.in_(database_ids)])

    async def find_by_extra_metadata(self, slug: str) -> list[ReportSchedule]:
        """Find report schedules containing slug in extra metadata."""
        escaped = slug.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        stmt = select(ReportSchedule).where(
            ReportSchedule.extra_json.like(f"%{escaped}%", escape="\\")
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def validate_unique_creation_method(
        self,
        dashboard_id: int | None = None,
        chart_id: int | None = None,
        user_id: int | None = None,
        report_id: int | None = None,
    ) -> bool:
        """Validate the current user has no chart/dashboard report attached.

        Scopes the uniqueness check to ``created_by_fk == get_user_id()``
        (the self-subscribe reports), then filters by the supplied dashboard
        and/or chart id.
        """
        conditions = [ReportSchedule.created_by_fk == user_id]
        if dashboard_id is not None:
            conditions.append(ReportSchedule.dashboard_id == dashboard_id)
        if chart_id is not None:
            conditions.append(ReportSchedule.chart_id == chart_id)
        if not conditions:
            return True
        stmt = select(ReportSchedule.id).where(*conditions)
        if report_id is not None:
            stmt = stmt.where(ReportSchedule.id != report_id)
        result = await self.session.execute(stmt)
        return result.scalars().first() is None

    async def bulk_delete_logs(
        self,
        report_schedule: ReportSchedule,
        from_date: datetime,
    ) -> int:
        stmt = delete(ReportExecutionLog).where(
            ReportExecutionLog.report_schedule_id == report_schedule.id,
            ReportExecutionLog.end_dttm < from_date,
        )
        result: CursorResult[Any] = await self.session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount


class AsyncReportExecutionLogDAO(BaseAsyncDAO[ReportExecutionLog]):
    model_cls = ReportExecutionLog
