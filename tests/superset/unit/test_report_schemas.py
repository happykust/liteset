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
"""Report schedule detail schema parity with upstream ``show_columns``."""

from __future__ import annotations

from types import SimpleNamespace

from superset.schemas.report import ReportDetailResult


def test_report_detail_includes_id() -> None:
    """``id`` is in upstream show_columns -> must appear inside ``result``.

    The Alerts/Reports edit modal reads it from the detail response.
    """
    obj = SimpleNamespace(id=17, name="my report", type="Report")
    result = ReportDetailResult.from_model(obj)
    assert result.id == 17
    assert result.name == "my report"
    assert result.type == "Report"
    # Relationship fields default cleanly when absent on the model.
    assert result.owners == []
    assert result.recipients == []
    assert result.chart is None
