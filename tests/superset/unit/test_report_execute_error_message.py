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
"""Regression for the report-execution ERROR ``error_message`` payload.

When a report/alert fails to deliver notifications, ``_send`` raises a
``ReportScheduleSystemErrorsException`` / ``ReportScheduleClientErrorsException``
with ``message=";".join(reasons)`` and ``ReportNotTriggeredErrorState.next``
writes an ERROR ``ReportExecutionLog`` whose ``error_message`` must carry those
reasons. NB these exception classes resolve ``__init__`` to
``CommandException`` (MRO), which does NOT accept ``errors=`` — so ``.errors``
is always ``[]`` here and ``str(ex)`` carries the joined message.

The bug: ``next`` unconditionally re-joined over the (empty) ``errors`` list →
BLANK ``error_message`` (the user-facing "last error" was wiped). The fix only
re-joins when ``errors`` is non-empty, else falls back to ``str(first_ex)``.
"""

from __future__ import annotations

from superset.commands.report_exceptions import (
    ReportScheduleClientErrorsException,
    ReportScheduleSystemErrorsException,
)


def _error_message(first_ex) -> str:
    """Replicates ReportNotTriggeredErrorState.next's error_message logic."""
    error_message = str(first_ex)
    if first_ex.errors:  # guarded: empty for these exceptions → keep str()
        error_message = ";".join(
            e.get("message", str(e))
            if isinstance(e, dict)
            else str(getattr(e, "message", e))
            for e in first_ex.errors
        )
    return error_message


def test_system_error_message_is_not_blank():
    ex = ReportScheduleSystemErrorsException(message="smtp down;slack 500")
    assert ex.errors == []  # CommandException MRO → no structured errors
    msg = _error_message(ex)
    assert msg == "smtp down;slack 500"
    assert msg  # the regression: must NOT be empty


def test_client_error_message_is_not_blank():
    ex = ReportScheduleClientErrorsException(message="bad recipient")
    assert _error_message(ex) == "bad recipient"
