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
"""Flask-free port of the dashboard-report create command integration tests.

Drives the real :class:`~superset.commands.report.CreateReportScheduleCommand`
(async) against a tabbed dashboard built through the integration factories, so
the tab-id validation (``_validate_report_extra``) runs against a real
``position_json``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from superset.commands.report import CreateReportScheduleCommand
from superset.commands.report_exceptions import ReportScheduleInvalidError
from superset.config import SupersetSettings
from superset.db.daos.report import AsyncReportScheduleDAO
from superset.models.dashboard import Dashboard
from superset.models.reports import (
    ReportCreationMethod,
    ReportRecipientType,
    ReportScheduleType,
)
from superset.security.manager import build_async_security_manager
from superset.utils import json
from tests.superset.integration import factories as f

# 1:1 port of the upstream tabbed_dashboard fixture position_json. Contains the
# tab ids referenced by the valid/invalid create cases (TAB-L1AA, TAB-L2AB).
TABBED_POSITION_JSON = {
    "DASHBOARD_VERSION_KEY": "v2",
    "GRID_ID": {
        "children": ["TABS-L1A", "TABS-L1B"],
        "id": "GRID_ID",
        "parents": ["ROOT_ID"],
        "type": "GRID",
    },
    "HEADER_ID": {
        "id": "HEADER_ID",
        "meta": {"text": "tabbed dashboard"},
        "type": "HEADER",
    },
    "ROOT_ID": {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"},
    "TAB-L1AA": {
        "children": [],
        "id": "TAB-L1AA",
        "meta": {
            "defaultText": "Tab title",
            "placeholder": "Tab title",
            "text": "Tab L1AA",
        },
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L1A"],
        "type": "TAB",
    },
    "TAB-L1AB": {
        "children": [],
        "id": "TAB-L1AB",
        "meta": {
            "defaultText": "Tab title",
            "placeholder": "Tab title",
            "text": "Tab L1AB",
        },
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L1A"],
        "type": "TAB",
    },
    "TABS-L1A": {
        "children": ["TAB-L1AA", "TAB-L1AB"],
        "id": "TABS-L1A",
        "meta": {},
        "parents": ["ROOT_ID", "GRID_ID"],
        "type": "TABS",
    },
    "TAB-L1BA": {
        "children": [],
        "id": "TAB-L1BA",
        "meta": {
            "defaultText": "Tab title",
            "placeholder": "Tab title",
            "text": "Tab L1B",
        },
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L1B"],
        "type": "TAB",
    },
    "TAB-L1BB": {
        "children": ["TABS-L2A"],
        "id": "TAB-L1BB",
        "meta": {
            "defaultText": "Tab title",
            "placeholder": "Tab title",
            "text": "Tab 2",
        },
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L1B"],
        "type": "TAB",
    },
    "TABS-L1B": {
        "children": ["TAB-L1BA", "TAB-L1BB"],
        "id": "TABS-L1B",
        "meta": {},
        "parents": ["ROOT_ID", "GRID_ID"],
        "type": "TABS",
    },
    "TAB-L2AA": {
        "children": [],
        "id": "TAB-L2AA",
        "meta": {
            "defaultText": "Tab title",
            "placeholder": "Tab title",
            "text": "Tab L2AA",
        },
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L2A"],
        "type": "TAB",
    },
    "TAB-L2AB": {
        "children": [],
        "id": "TAB-L2AB",
        "meta": {
            "defaultText": "Tab title",
            "placeholder": "Tab title",
            "text": "Tab L2AB",
        },
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L2A"],
        "type": "TAB",
    },
    "TABS-L2A": {
        "children": ["TAB-L2AA", "TAB-L2AB"],
        "id": "TABS-L2A",
        "meta": {},
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L1BB"],
        "type": "TABS",
    },
}

# Carries the upstream EMAIL recipient (1:1 with the upstream DEFAULTS). The
# async DAO now persists recipients via the FK (``session.add``) instead of
# appending to the flushed report's lazy="select" collection, so this no longer
# triggers a MissingGreenlet under a bare AsyncSession.
DASHBOARD_REPORT_SCHEDULE_DEFAULTS = {
    "type": ReportScheduleType.REPORT,
    "description": "description",
    "crontab": "0 9 * * *",
    "creation_method": ReportCreationMethod.ALERTS_REPORTS,
    "grace_period": 14400,
    "working_timeout": 3600,
    "recipients": [
        {
            "type": ReportRecipientType.EMAIL,
            "recipient_config_json": {"target": "target@email.com"},
        }
    ],
}


async def _make_tabbed_dashboard(session: AsyncSession) -> Dashboard:
    return await f.create_dashboard(
        session,
        dashboard_title="Test tabbed dash",
        slug=None,
        position_json=json.dumps(TABBED_POSITION_JSON),
    )


def _make_command(
    session: AsyncSession, data: dict
) -> CreateReportScheduleCommand:
    settings = SupersetSettings()  # type: ignore[call-arg]
    sm = build_async_security_manager(session, settings)
    return CreateReportScheduleCommand(
        AsyncReportScheduleDAO(session),
        data,
        user_id=None,
        security_manager=sm,
    )


@pytest.mark.usefixtures("tabbed_dashboard")
async def test_accept_valid_tab_ids(db_session: AsyncSession) -> None:
    dashboard = await _make_tabbed_dashboard(db_session)
    report_schedule = await _make_command(
        db_session,
        {
            **DASHBOARD_REPORT_SCHEDULE_DEFAULTS,
            "name": "tabbed dashboard report (valid tabs id)",
            "dashboard": dashboard.id,
            "extra": {"dashboard": {"activeTabs": ["TAB-L1AA", "TAB-L2AB"]}},
        },
    ).execute()
    assert report_schedule.extra == {
        "dashboard": {"activeTabs": ["TAB-L1AA", "TAB-L2AB"]}
    }
    # The EMAIL recipient is persisted (FK-based, no MissingGreenlet).
    recipients = await report_schedule.awaitable_attrs.recipients
    assert [r.type for r in recipients] == [ReportRecipientType.EMAIL]


@pytest.mark.usefixtures("tabbed_dashboard")
async def test_raise_exception_for_invalid_tab_ids(db_session: AsyncSession) -> None:
    dashboard = await _make_tabbed_dashboard(db_session)

    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await _make_command(
            db_session,
            {
                **DASHBOARD_REPORT_SCHEDULE_DEFAULTS,
                "name": "tabbed dashboard report (invalid tab ids)",
                "dashboard": dashboard.id,
                "extra": {"dashboard": {"activeTabs": ["TAB-INVALID_ID"]}},
            },
        ).execute()
    assert "Invalid tab ids" in str(exc_info.value.normalized_messages())

    with pytest.raises(ReportScheduleInvalidError) as exc_info:
        await _make_command(
            db_session,
            {
                **DASHBOARD_REPORT_SCHEDULE_DEFAULTS,
                "name": "tabbed dashboard report (invalid tab ids in anchor)",
                "dashboard": dashboard.id,
                "extra": {
                    "dashboard": {
                        "activeTabs": ["TAB-L1AA"],
                        "anchor": "TAB-INVALID_ID",
                    }
                },
            },
        ).execute()
    assert "Invalid tab ids" in str(exc_info.value.normalized_messages())
