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
"""ALERT_REPORTS feature-flag gate on report + report-log controllers.

Every report and report-log endpoint returns 404 when the ALERT_REPORTS
feature flag is disabled.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from litestar.exceptions import NotFoundException

from superset.controllers.report import ReportScheduleController
from superset.controllers.report_log import ReportExecutionLogController
from superset.guards.rbac import require_feature_flag
from superset.utils.feature_flags import feature_flag_manager


def test_require_feature_flag_disabled_raises_404(monkeypatch):
    """Guard raises NotFoundException (→404) when the flag is disabled."""
    monkeypatch.setattr(
        feature_flag_manager,
        "is_feature_enabled",
        lambda feature: False,
    )
    guard = require_feature_flag("ALERT_REPORTS")
    with pytest.raises(NotFoundException):
        guard(MagicMock(), MagicMock())


def test_require_feature_flag_enabled_passes(monkeypatch):
    """Guard is a no-op when the flag is enabled."""
    monkeypatch.setattr(
        feature_flag_manager,
        "is_feature_enabled",
        lambda feature: feature == "ALERT_REPORTS",
    )
    guard = require_feature_flag("ALERT_REPORTS")
    # Must not raise.
    assert guard(MagicMock(), MagicMock()) is None


def test_require_feature_flag_gates_only_requested_flag(monkeypatch):
    """Guard only consults its own feature name."""
    seen = []

    def _is_enabled(feature: str) -> bool:
        seen.append(feature)
        return True

    monkeypatch.setattr(feature_flag_manager, "is_feature_enabled", _is_enabled)
    require_feature_flag("ALERT_REPORTS")(MagicMock(), MagicMock())
    assert seen == ["ALERT_REPORTS"]


@pytest.mark.parametrize(
    "controller_cls",
    [ReportScheduleController, ReportExecutionLogController],
)
def test_controllers_wire_alert_reports_gate(controller_cls, monkeypatch):
    """Both controllers carry the ALERT_REPORTS gate at the controller level.

    A controller-level guard applies to every route on the controller —
    the Litestar-idiomatic equivalent of FAB's ``@before_request`` hook.
    """
    monkeypatch.setattr(
        feature_flag_manager,
        "is_feature_enabled",
        lambda feature: False,
    )
    guards = getattr(controller_cls, "guards", None) or []
    # The guard is a closure; exercise it with the flag disabled to confirm
    # at least one of the controller guards gates on ALERT_REPORTS → 404.
    import contextlib

    gated = False
    for guard in guards:
        with contextlib.suppress(Exception):
            # An ALERT_REPORTS gate raises NotFoundException when disabled.
            try:
                guard(MagicMock(), MagicMock())
            except NotFoundException:
                gated = True
                break
    # With the flag at its default (False), the gate must trip.
    assert gated, f"{controller_cls.__name__} is missing the ALERT_REPORTS gate"
