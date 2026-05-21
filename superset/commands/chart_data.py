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
"""Backwards-compatible re-export shim.

The chart-data commands now live in
``superset.commands.chart.data.get_data_command`` mirroring the
original ``superset_old/commands/chart/data/`` package layout.

This module re-exports the canonical names so existing call sites keep
working until they're migrated.  New code should import from
``superset.commands.chart.data.get_data_command`` directly.
"""

from __future__ import annotations

from superset.commands.chart.data.get_data_command import ChartDataCommand

__all__ = ("ChartDataCommand",)
