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
"""Shared task utilities for Liteset.

Replaces ``superset/tasks/utils.py``. The ``get_executor`` logic is
preserved as-is since it is used by cache warming, thumbnails, and
alert/report tasks.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from celery.utils.log import get_task_logger

from liteset.tasks.exceptions import ExecutorNotFoundError, InvalidExecutorError
from liteset.tasks.types import ChosenExecutor, Executor, ExecutorType, FixedExecutor

if TYPE_CHECKING:
    from liteset.models.dashboard import Dashboard
    from liteset.models.reports import ReportSchedule
    from liteset.models.slice import Slice

logger = get_task_logger(__name__)
logger.setLevel(logging.INFO)


def get_executor(
    executors: list[Executor],
    model: Dashboard | ReportSchedule | Slice,
    current_user: str | None = None,
) -> ChosenExecutor:
    """Extract the user that should execute a scheduled task.

    Iterates *executors* in order and returns the first matching user.

    :param executors: Executor types in descending priority.
    :param model: The underlying object (chart, dashboard, or report).
    :param current_user: Username of the user that initiated the task.
    :returns: ``(ExecutorType, username)`` tuple.
    :raises ExecutorNotFoundError: If no valid user is found.
    :raises InvalidExecutorError: If ``FIXED_USER`` is used without
        :class:`FixedExecutor`.
    """
    owners = model.owners
    owner_dict = {owner.id: owner for owner in owners}

    for executor in executors:
        if isinstance(executor, FixedExecutor):
            return ExecutorType.FIXED_USER, executor.username
        if executor == ExecutorType.FIXED_USER:
            raise InvalidExecutorError()
        if executor == ExecutorType.CURRENT_USER and current_user:
            return executor, current_user
        if executor == ExecutorType.CREATOR_OWNER:
            if (user := model.created_by) and (owner := owner_dict.get(user.id)):
                return executor, owner.username
        if executor == ExecutorType.CREATOR:
            if user := model.created_by:
                return executor, user.username
        if executor == ExecutorType.MODIFIER_OWNER:
            if (user := model.changed_by) and (owner := owner_dict.get(user.id)):
                return executor, owner.username
        if executor == ExecutorType.MODIFIER:
            if user := model.changed_by:
                return executor, user.username
        if executor == ExecutorType.OWNER:
            owners = model.owners
            if len(owners) == 1:
                return executor, owners[0].username
            if len(owners) > 1:
                if modifier := model.changed_by:
                    if modifier and (user := owner_dict.get(modifier.id)):
                        return executor, user.username
                if creator := model.created_by:
                    if creator and (user := owner_dict.get(creator.id)):
                        return executor, user.username
                return executor, owners[0].username

    raise ExecutorNotFoundError()
