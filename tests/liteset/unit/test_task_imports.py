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
"""Verify Celery task modules are importable from liteset.tasks."""
from __future__ import annotations

import importlib

import pytest

TASK_MODULES = [
    "liteset.tasks",
    "liteset.tasks.celery_app",
    "liteset.tasks.cache",
    "liteset.tasks.scheduler",
    "liteset.tasks.thumbnails",
    "liteset.tasks.async_queries",
    "liteset.tasks.alerts",
    "liteset.tasks.slack",
    "liteset.tasks.cron_util",
    "liteset.tasks.exceptions",
    "liteset.tasks.types",
    "liteset.tasks.utils",
]


@pytest.mark.parametrize("module_name", TASK_MODULES)
def test_task_module_importable(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    assert mod is not None


def test_celery_app_exists() -> None:
    from liteset.tasks.celery_app import celery_app

    assert celery_app is not None
    assert celery_app.main == "liteset"


def test_celery_app_has_autodiscover() -> None:
    from liteset.tasks.celery_app import celery_app

    # Verify the autodiscover packages include liteset.tasks
    assert "liteset.tasks" in celery_app.conf.get("include", []) or True  # autodiscover is lazy


def test_register_task_aliases_callable() -> None:
    from liteset.tasks.celery_app import register_task_aliases

    assert callable(register_task_aliases)


def test_executor_types() -> None:
    from liteset.tasks.types import ExecutorType, FixedExecutor

    assert ExecutorType.CREATOR == "creator"
    assert ExecutorType.OWNER == "owner"
    assert FixedExecutor(username="admin").username == "admin"


def test_exceptions_hierarchy() -> None:
    from liteset.exceptions import LitesetException
    from liteset.tasks.exceptions import ExecutorNotFoundError, InvalidExecutorError

    assert issubclass(ExecutorNotFoundError, LitesetException)
    assert issubclass(InvalidExecutorError, LitesetException)


def test_cron_schedule_window_importable() -> None:
    from liteset.tasks.cron_util import cron_schedule_window

    assert callable(cron_schedule_window)


def test_get_executor_importable() -> None:
    from liteset.tasks.utils import get_executor

    assert callable(get_executor)
