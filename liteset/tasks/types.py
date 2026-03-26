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
"""Type definitions for Liteset task executors.

Replaces ``superset/tasks/types.py``. Uses :class:`enum.StrEnum` directly
(Python 3.11+) instead of the ``superset.utils.backports`` shim.
"""
from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple


class FixedExecutor(NamedTuple):
    """A fixed user account used for task execution."""

    username: str


class ExecutorType(StrEnum):
    """Which user should async tasks be executed as.

    For Alerts & Reports the "model" refers to the ``ReportSchedule`` object.
    For Thumbnails the "model" refers to the ``Slice`` or ``Dashboard`` object.
    """

    FIXED_USER = "fixed_user"
    CREATOR = "creator"
    CREATOR_OWNER = "creator_owner"
    CURRENT_USER = "current_user"
    MODIFIER = "modifier"
    MODIFIER_OWNER = "modifier_owner"
    OWNER = "owner"


Executor = FixedExecutor | ExecutorType

# Alias type: (executor_type, username) tuple returned by ``get_executor``.
ChosenExecutor = tuple[ExecutorType, str]
