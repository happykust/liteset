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
"""Tests for AsyncReportScheduleDAO using simplified test models."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    delete,
    ForeignKey,
    Integer,
    select,
    String,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from superset.db.base_dao import BaseAsyncDAO


class Base(DeclarativeBase):
    pass


class FakeReport(Base):
    __tablename__ = "fake_reports"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(250), nullable=False)
    type = Column(String(50), nullable=False)
    chart_id = Column(Integer, nullable=True)
    dashboard_id = Column(Integer, nullable=True)
    database_id = Column(Integer, nullable=True)
    active = Column(Boolean, default=True)
    extra_json = Column(Text, nullable=True)


class FakeRecipient(Base):
    __tablename__ = "fake_recipients"
    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(50), nullable=False)
    recipient_config_json = Column(Text, nullable=True)
    report_schedule_id = Column(Integer, ForeignKey("fake_reports.id"), nullable=False)


class FakeExecutionLog(Base):
    __tablename__ = "fake_execution_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    report_schedule_id = Column(Integer, ForeignKey("fake_reports.id"), nullable=False)
    state = Column(String(50), nullable=False)
    start_dttm = Column(DateTime, nullable=True)
    end_dttm = Column(DateTime, nullable=True)
    error_message = Column(String(500), nullable=True)


class FakeReportDAO(BaseAsyncDAO[FakeReport]):
    model_cls = FakeReport

    async def find_by_chart_id(self, chart_id: int) -> list[FakeReport]:
        return await self.find_all([FakeReport.chart_id == chart_id])

    async def find_by_dashboard_id(self, dashboard_id: int) -> list[FakeReport]:
        return await self.find_all([FakeReport.dashboard_id == dashboard_id])

    async def find_by_database_id(self, database_id: int) -> list[FakeReport]:
        return await self.find_all([FakeReport.database_id == database_id])

    async def validate_update_uniqueness(
        self, name: str, report_type: str, report_id: int | None = None
    ) -> bool:
        stmt = select(FakeReport.id).where(
            FakeReport.name == name, FakeReport.type == report_type
        )
        if report_id is not None:
            stmt = stmt.where(FakeReport.id != report_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def create_with_recipients(
        self, attributes: dict, recipients: list[dict]
    ) -> FakeReport:
        report = await self.create(attributes)
        await self.session.flush()
        for r in recipients:
            rec = FakeRecipient(
                type=r["type"],
                recipient_config_json=r.get("config", ""),
                report_schedule_id=report.id,
            )
            self.session.add(rec)
        return report

    async def find_by_chart_ids(self, chart_ids: list[int]) -> list[FakeReport]:
        if not chart_ids:
            return []
        return await self.find_all([FakeReport.chart_id.in_(chart_ids)])

    async def find_by_dashboard_ids(self, dashboard_ids: list[int]) -> list[FakeReport]:
        if not dashboard_ids:
            return []
        return await self.find_all([FakeReport.dashboard_id.in_(dashboard_ids)])

    async def find_by_database_ids(self, database_ids: list[int]) -> list[FakeReport]:
        if not database_ids:
            return []
        return await self.find_all([FakeReport.database_id.in_(database_ids)])

    async def find_by_extra_metadata(self, slug: str) -> list[FakeReport]:
        stmt = select(FakeReport).where(FakeReport.extra_json.like(f"%{slug}%"))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def validate_unique_creation_method(
        self,
        dashboard_id: int | None = None,
        chart_id: int | None = None,
        report_id: int | None = None,
    ) -> bool:
        conditions = []
        if dashboard_id is not None:
            conditions.append(FakeReport.dashboard_id == dashboard_id)
        if chart_id is not None:
            conditions.append(FakeReport.chart_id == chart_id)
        if not conditions:
            return True
        stmt = select(FakeReport.id).where(*conditions)
        if report_id is not None:
            stmt = stmt.where(FakeReport.id != report_id)
        result = await self.session.execute(stmt)
        return result.scalars().first() is None

    async def find_active(self) -> list[FakeReport]:
        return await self.find_all([FakeReport.active.is_(True)])

    async def find_last_success_log(
        self, report: FakeReport
    ) -> FakeExecutionLog | None:
        stmt = (
            select(FakeExecutionLog)
            .where(
                FakeExecutionLog.report_schedule_id == report.id,
                FakeExecutionLog.state == "success",
            )
            .order_by(FakeExecutionLog.end_dttm.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_last_entered_working_log(
        self, report: FakeReport
    ) -> FakeExecutionLog | None:
        stmt = (
            select(FakeExecutionLog)
            .where(
                FakeExecutionLog.report_schedule_id == report.id,
                FakeExecutionLog.state == "Working",
                FakeExecutionLog.error_message.is_(None),
            )
            .order_by(FakeExecutionLog.end_dttm.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_last_error_notification(
        self, report: FakeReport
    ) -> FakeExecutionLog | None:
        # Find last error notification
        error_log_stmt = (
            select(FakeExecutionLog)
            .where(
                FakeExecutionLog.report_schedule_id == report.id,
                FakeExecutionLog.state == "error",
            )
            .order_by(FakeExecutionLog.end_dttm.desc())
            .limit(1)
        )
        result = await self.session.execute(error_log_stmt)
        error_log = result.scalars().one_or_none()
        if error_log is None:
            return None

        # Check if there is a non-error log after it
        success_after_stmt = (
            select(FakeExecutionLog)
            .where(
                FakeExecutionLog.report_schedule_id == report.id,
                FakeExecutionLog.state == "success",
                FakeExecutionLog.end_dttm > error_log.end_dttm,
            )
            .limit(1)
        )
        result = await self.session.execute(success_after_stmt)
        if result.scalars().one_or_none() is not None:
            return None
        return error_log

    async def update_with_recipients(
        self, report: FakeReport, attributes: dict, recipients: list[dict]
    ) -> FakeReport:
        for key, value in attributes.items():
            setattr(report, key, value)
        # Delete old recipients
        del_stmt = delete(FakeRecipient).where(
            FakeRecipient.report_schedule_id == report.id
        )
        await self.session.execute(del_stmt)
        # Add new recipients
        for r in recipients:
            rec = FakeRecipient(
                type=r["type"],
                recipient_config_json=r.get("config", ""),
                report_schedule_id=report.id,
            )
            self.session.add(rec)
        return report

    async def bulk_delete_logs(self, report: FakeReport, from_date: datetime) -> int:
        stmt = delete(FakeExecutionLog).where(
            FakeExecutionLog.report_schedule_id == report.id,
            FakeExecutionLog.end_dttm < from_date,
        )
        result = await self.session.execute(stmt)
        return result.rowcount


@pytest.fixture
async def async_session():
    from tests.superset.conftest import create_test_session

    async with create_test_session(Base) as session:
        yield session


async def test_create_report(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    r = await dao.create({"name": "Daily Report", "type": "alert", "active": True})
    await async_session.flush()
    assert r.id is not None


async def test_find_by_chart_id(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    await dao.create({"name": "R1", "type": "report", "chart_id": 10})
    await dao.create({"name": "R2", "type": "report", "chart_id": 20})
    await async_session.flush()

    results = await dao.find_by_chart_id(10)
    assert len(results) == 1
    assert results[0].name == "R1"


async def test_find_by_dashboard_id(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    await dao.create({"name": "R1", "type": "report", "dashboard_id": 5})
    await async_session.flush()
    results = await dao.find_by_dashboard_id(5)
    assert len(results) == 1


async def test_validate_update_uniqueness(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    r = await dao.create({"name": "Unique", "type": "alert"})
    await async_session.flush()

    assert await dao.validate_update_uniqueness("Unique", "alert", r.id) is True
    assert await dao.validate_update_uniqueness("Unique", "alert") is False
    assert await dao.validate_update_uniqueness("New Name", "alert") is True


async def test_create_with_recipients(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    report = await dao.create_with_recipients(
        {"name": "With Recip", "type": "report"},
        [
            {"type": "email", "config": "user@example.com"},
            {"type": "slack", "config": "#channel"},
        ],
    )
    await async_session.flush()

    stmt = select(FakeRecipient).where(FakeRecipient.report_schedule_id == report.id)
    result = await async_session.execute(stmt)
    recipients = list(result.scalars().all())
    assert len(recipients) == 2


async def test_find_active(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    await dao.create({"name": "Active", "type": "report", "active": True})
    await dao.create({"name": "Inactive", "type": "report", "active": False})
    await async_session.flush()

    active = await dao.find_active()
    assert len(active) == 1
    assert active[0].name == "Active"


async def test_find_last_success_log(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    report = await dao.create({"name": "Log Test", "type": "report"})
    await async_session.flush()

    log1 = FakeExecutionLog(
        report_schedule_id=report.id,
        state="success",
        end_dttm=datetime(2025, 1, 1),
    )
    log2 = FakeExecutionLog(
        report_schedule_id=report.id,
        state="success",
        end_dttm=datetime(2025, 6, 1),
    )
    log3 = FakeExecutionLog(
        report_schedule_id=report.id,
        state="error",
        end_dttm=datetime(2025, 7, 1),
    )
    async_session.add_all([log1, log2, log3])
    await async_session.flush()

    last_success = await dao.find_last_success_log(report)
    assert last_success is not None
    assert last_success.end_dttm == datetime(2025, 6, 1)


async def test_bulk_delete_logs(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    report = await dao.create({"name": "Delete Logs", "type": "report"})
    await async_session.flush()

    for i in range(5):
        log = FakeExecutionLog(
            report_schedule_id=report.id,
            state="success",
            end_dttm=datetime(2025, 1, 1) + timedelta(days=i * 30),
        )
        async_session.add(log)
    await async_session.flush()

    # Delete logs before April 2025
    deleted = await dao.bulk_delete_logs(report, datetime(2025, 4, 1))
    await async_session.flush()
    assert deleted == 3  # Jan, Feb, Mar


async def test_find_last_entered_working_log(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    report = await dao.create({"name": "R", "type": "report", "active": True})
    await async_session.flush()

    log = FakeExecutionLog(
        report_schedule_id=report.id,
        state="Working",
        start_dttm=datetime(2025, 1, 1),
        end_dttm=datetime(2025, 1, 1),
        error_message=None,
    )
    async_session.add(log)
    await async_session.flush()

    result = await dao.find_last_entered_working_log(report)
    assert result is not None
    assert result.state == "Working"


async def test_find_last_entered_working_log_excludes_errors(
    async_session: AsyncSession,
) -> None:
    dao = FakeReportDAO(async_session)
    report = await dao.create({"name": "R", "type": "report", "active": True})
    await async_session.flush()

    log = FakeExecutionLog(
        report_schedule_id=report.id,
        state="Working",
        start_dttm=datetime(2025, 1, 1),
        end_dttm=datetime(2025, 1, 1),
        error_message="some error",
    )
    async_session.add(log)
    await async_session.flush()

    result = await dao.find_last_entered_working_log(report)
    assert result is None


async def test_find_last_error_notification_no_errors(
    async_session: AsyncSession,
) -> None:
    dao = FakeReportDAO(async_session)
    report = await dao.create({"name": "R", "type": "report"})
    await async_session.flush()

    log = FakeExecutionLog(
        report_schedule_id=report.id,
        state="success",
        end_dttm=datetime(2025, 1, 1),
    )
    async_session.add(log)
    await async_session.flush()

    result = await dao.find_last_error_notification(report)
    assert result is None


async def test_find_last_error_notification_error_without_recovery(
    async_session: AsyncSession,
) -> None:
    dao = FakeReportDAO(async_session)
    report = await dao.create({"name": "R", "type": "report"})
    await async_session.flush()

    error_log = FakeExecutionLog(
        report_schedule_id=report.id,
        state="error",
        end_dttm=datetime(2025, 6, 1),
        error_message="Something failed",
    )
    async_session.add(error_log)
    await async_session.flush()

    result = await dao.find_last_error_notification(report)
    assert result is not None
    assert result.state == "error"


async def test_find_last_error_notification_error_with_recovery(
    async_session: AsyncSession,
) -> None:
    dao = FakeReportDAO(async_session)
    report = await dao.create({"name": "R", "type": "report"})
    await async_session.flush()

    error_log = FakeExecutionLog(
        report_schedule_id=report.id,
        state="error",
        end_dttm=datetime(2025, 6, 1),
    )
    success_log = FakeExecutionLog(
        report_schedule_id=report.id,
        state="success",
        end_dttm=datetime(2025, 7, 1),
    )
    async_session.add_all([error_log, success_log])
    await async_session.flush()

    result = await dao.find_last_error_notification(report)
    assert result is None


async def test_update_with_recipients(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    report = await dao.create_with_recipients(
        {"name": "R", "type": "report"},
        [{"type": "email", "config": "old@example.com"}],
    )
    await async_session.flush()

    # Verify initial recipients
    stmt = select(FakeRecipient).where(FakeRecipient.report_schedule_id == report.id)
    result = await async_session.execute(stmt)
    assert len(list(result.scalars().all())) == 1

    # Update with new recipients
    await dao.update_with_recipients(
        report,
        {"name": "Updated R"},
        [
            {"type": "slack", "config": "#alerts"},
            {"type": "email", "config": "new@example.com"},
        ],
    )
    await async_session.flush()

    assert report.name == "Updated R"
    stmt = select(FakeRecipient).where(FakeRecipient.report_schedule_id == report.id)
    result = await async_session.execute(stmt)
    recipients = list(result.scalars().all())
    assert len(recipients) == 2
    types = {r.type for r in recipients}
    assert types == {"slack", "email"}


async def test_find_by_chart_ids(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    await dao.create({"name": "R1", "type": "report", "chart_id": 10})
    await dao.create({"name": "R2", "type": "report", "chart_id": 20})
    await dao.create({"name": "R3", "type": "report", "chart_id": 30})
    await async_session.flush()

    results = await dao.find_by_chart_ids([10, 20])
    assert len(results) == 2
    names = {r.name for r in results}
    assert names == {"R1", "R2"}


async def test_find_by_chart_ids_empty(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    results = await dao.find_by_chart_ids([])
    assert results == []


async def test_find_by_dashboard_ids(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    await dao.create({"name": "R1", "type": "report", "dashboard_id": 5})
    await dao.create({"name": "R2", "type": "report", "dashboard_id": 6})
    await async_session.flush()

    results = await dao.find_by_dashboard_ids([5, 6])
    assert len(results) == 2


async def test_find_by_database_ids(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    await dao.create({"name": "R1", "type": "report", "database_id": 100})
    await async_session.flush()

    results = await dao.find_by_database_ids([100, 200])
    assert len(results) == 1


async def test_find_by_extra_metadata(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    await dao.create(
        {
            "name": "R1",
            "type": "report",
            "extra_json": '{"dashboard_slug": "my-dash"}',
        }
    )
    await dao.create(
        {
            "name": "R2",
            "type": "report",
            "extra_json": '{"dashboard_slug": "other"}',
        }
    )
    await async_session.flush()

    results = await dao.find_by_extra_metadata("my-dash")
    assert len(results) == 1
    assert results[0].name == "R1"


async def test_validate_unique_creation_method(async_session: AsyncSession) -> None:
    dao = FakeReportDAO(async_session)
    r = await dao.create(
        {
            "name": "R1",
            "type": "report",
            "dashboard_id": 5,
            "chart_id": 10,
        }
    )
    await async_session.flush()

    # Same combo should fail
    assert (
        await dao.validate_unique_creation_method(
            dashboard_id=5,
            chart_id=10,
        )
        is False
    )

    # Same combo but excluding self should pass
    assert (
        await dao.validate_unique_creation_method(
            dashboard_id=5,
            chart_id=10,
            report_id=r.id,
        )
        is True
    )

    # Different combo should pass
    assert (
        await dao.validate_unique_creation_method(
            dashboard_id=5,
            chart_id=99,
        )
        is True
    )

    # No conditions should always pass
    assert await dao.validate_unique_creation_method() is True
