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

import logging
import uuid
from contextlib import nullcontext
from inspect import isclass
from typing import Any, Optional
from unittest.mock import AsyncMock, call, Mock, patch

import pytest

from superset.utils import core as utils_core, decorators
from superset.utils.backports import StrEnum


class ResponseValues(StrEnum):
    FAIL = "fail"
    WARN = "warn"
    OK = "ok"


def test_debounce() -> None:
    mock = Mock()

    @decorators.debounce()
    def myfunc(arg1: int, arg2: int, kwarg1: str = "abc", kwarg2: int = 2) -> int:
        mock(arg1, kwarg1)
        return arg1 + arg2 + kwarg2

    # should be called only once when arguments don't change
    myfunc(1, 1)
    myfunc(1, 1)
    result = myfunc(1, 1)
    mock.assert_called_once_with(1, "abc")
    assert result == 4

    # kwarg order shouldn't matter
    myfunc(1, 0, kwarg2=2, kwarg1="haha")
    result = myfunc(1, 0, kwarg1="haha", kwarg2=2)
    mock.assert_has_calls([call(1, "abc"), call(1, "haha")])
    assert result == 3


@pytest.mark.parametrize(
    "response_value, expected_exception, expected_result",
    [
        (ResponseValues.OK, None, "custom.prefix.ok"),
        (ResponseValues.FAIL, ValueError, "custom.prefix.error"),
        (ResponseValues.WARN, FileNotFoundError, "custom.prefix.warn"),
    ],
)
def test_statsd_gauge(
    response_value: str, expected_exception: Optional[Exception], expected_result: str
) -> None:
    @decorators.statsd_gauge("custom.prefix")
    def my_func(response: ResponseValues, *args: Any, **kwargs: Any) -> str:
        if response == ResponseValues.FAIL:
            raise ValueError("Error")
        if response == ResponseValues.WARN:
            raise FileNotFoundError("Not found")
        return "OK"

    # The Liteset port resolves the stats logger lazily from
    # ``superset.extensions.stats_logger_manager`` instead of reading
    # ``app.config["STATS_LOGGER"]``.
    with patch("superset.extensions.stats_logger_manager.instance.gauge") as mock:
        cm = (
            pytest.raises(expected_exception)
            if isclass(expected_exception) and issubclass(expected_exception, Exception)
            else nullcontext()
        )

        with cm:
            my_func(response_value, 1, 2)
            mock.assert_called_once_with(expected_result, 1)


def test_context_decorator() -> None:
    """Test the ``logs_context`` decorator.

    The Liteset port replaces the legacy ``flask.g.logs_context`` with a
    per-task :func:`superset.utils.core.get_logs_context` ContextVar, so the
    test reads/writes that dict instead of patching ``decorators.g``.
    """

    def logs_context() -> dict[str, Any]:
        return utils_core.get_logs_context()

    def reset() -> None:
        utils_core.reset_logs_context()

    @decorators.logs_context()
    def myfunc(*args, **kwargs) -> str:
        return "test"

    @decorators.logs_context(slice_id=1, dashboard_id=1, execution_id=uuid.uuid4())
    def myfunc_with_kwargs(*args, **kwargs) -> str:
        return "test"

    @decorators.logs_context(bad_context=1)
    def myfunc_with_dissallowed_kwargs(*args, **kwargs) -> str:
        return "test"

    @decorators.logs_context(
        context_func=lambda *args, **kwargs: {"slice_id": kwargs["chart_id"]}
    )
    def myfunc_with_context(*args, **kwargs) -> str:
        return "test"

    ### should not add any data to the logs_context scope
    reset()
    myfunc(1, 1)
    assert logs_context() == {}

    ### should add dashboard_id to the logs_context scope
    reset()
    myfunc(1, 1, dashboard_id=1)
    assert logs_context() == {"dashboard_id": 1}

    ### should add slice_id to the logs_context scope
    reset()
    myfunc(1, 1, slice_id=1)
    assert logs_context() == {"slice_id": 1}

    ### should add execution_id to the logs_context scope
    reset()
    myfunc(1, 1, execution_id=1)
    assert logs_context() == {"execution_id": 1}

    ### should add all three to the logs_context scope
    reset()
    myfunc(1, 1, dashboard_id=1, slice_id=1, execution_id=1)
    assert logs_context() == {
        "dashboard_id": 1,
        "slice_id": 1,
        "execution_id": 1,
    }

    ### should overwrite existing values in the logs_context scope
    reset()
    logs_context().update({"dashboard_id": 2, "slice_id": 2, "execution_id": 2})
    myfunc(1, 1, dashboard_id=3, slice_id=3, execution_id=3)
    assert logs_context() == {
        "dashboard_id": 3,
        "slice_id": 3,
        "execution_id": 3,
    }

    ### Test when logs_context already exists
    reset()
    logs_context().update({"slice_id": 2, "dashboard_id": 2})
    args = (3, 4)
    kwargs = {"slice_id": 3, "dashboard_id": 3}
    myfunc(*args, **kwargs)
    assert logs_context() == {"slice_id": 3, "dashboard_id": 3}

    ### Test when kwargs contain additional keys
    reset()
    args = (1, 2)
    kwargs = {
        "slice_id": 1,
        "dashboard_id": 1,
        "dataset_id": 1,
        "execution_id": 1,
        "report_schedule_id": 1,
        "extra_key": 1,
    }
    myfunc(*args, **kwargs)
    assert logs_context() == {
        "slice_id": 1,
        "dashboard_id": 1,
        "dataset_id": 1,
        "execution_id": 1,
        "report_schedule_id": 1,
    }

    ### should not add a value that does not exist in the logs_context scope
    reset()
    myfunc_with_dissallowed_kwargs()
    assert logs_context() == {}

    ### should be able to add values to the decorator function directly
    reset()
    myfunc_with_kwargs()
    assert logs_context()["dashboard_id"] == 1
    assert logs_context()["slice_id"] == 1
    assert isinstance(logs_context()["execution_id"], uuid.UUID)

    ### should be able to add values to the decorator function directly
    # and it will overwrite any kwargs passed into the decorated function
    reset()
    myfunc_with_kwargs(execution_id=4)

    assert logs_context()["dashboard_id"] == 1
    assert logs_context()["slice_id"] == 1
    assert isinstance(logs_context()["execution_id"], uuid.UUID)

    ### should be able to pass a callable context to the decorator
    reset()
    myfunc_with_context(chart_id=1)
    assert logs_context() == {"slice_id": 1}

    ### Test when context_func returns additional keys
    # it should use the context_func values
    reset()
    args = (1, 2)
    kwargs = {"slice_id": 1, "dashboard_id": 1}

    @decorators.logs_context(
        context_func=lambda *args, **kwargs: {
            "slice_id": 2,
            "dashboard_id": 2,
            "dataset_id": 2,
            "execution_id": 2,
            "report_schedule_id": 2,
            "extra_key": 2,
        }
    )
    def myfunc_with_extra_keys_context(*args, **kwargs) -> str:
        return "test"

    myfunc_with_extra_keys_context(
        *args,
        **kwargs,
    )
    assert logs_context() == {
        "slice_id": 2,
        "dashboard_id": 2,
        "dataset_id": 2,
        "execution_id": 2,
        "report_schedule_id": 2,
    }

    ### Test when context_func does not return a dictionary
    reset()

    @decorators.logs_context(context_func=lambda: "foo")  # type: ignore
    def myfunc_with_bad_return_value() -> str:
        return "test"

    myfunc_with_bad_return_value()
    assert logs_context() == {}

    ### Test when context_func is not callable
    reset()

    @decorators.logs_context(context_func="foo")  # type: ignore
    def context_func_not_callable() -> str:
        return "test"

    context_func_not_callable()
    assert logs_context() == {}

    reset()


class ListHandler(logging.Handler):
    """
    Simple logging handler that stores records in a list.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.log_records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.log_records.append(record)

    def reset(self) -> None:
        self.log_records = []


def test_suppress_logging() -> None:
    """
    Test the `suppress_logging` decorator.
    """
    handler = ListHandler()
    logger = logging.getLogger("test-logger")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    def func() -> None:
        logger.error("error")
        logger.critical("critical")

    func()
    assert len(handler.log_records) == 2

    handler.log_records = []
    decorated = decorators.suppress_logging("test-logger")(func)
    decorated()
    assert len(handler.log_records) == 1
    assert handler.log_records[0].levelname == "CRITICAL"

    handler.log_records = []
    decorated = decorators.suppress_logging("test-logger", logging.CRITICAL + 1)(func)
    decorated()
    assert len(handler.log_records) == 0


@pytest.mark.asyncio
async def test_transacation_commit() -> None:
    """
    Test the `transaction` decorator when the function completes successfully.

    The Liteset port drives an :class:`AsyncSession` resolved from
    ``self.session`` instead of the Flask global ``superset.db.session``, so
    the decorated function is an instance method and the session is a per-
    instance ``AsyncMock``. The commit-on-success contract is otherwise 1:1.
    """
    session = Mock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    class Command:
        def __init__(self) -> None:
            self.session = session

        @decorators.transaction()
        async def run(self) -> int:
            return 42

    result = await Command().run()
    assert result == 42
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_transacation_rollback() -> None:
    """
    Test the `transaction` decorator when the function raises an exception.

    Ported to the Liteset per-instance ``AsyncSession`` contract; the
    rollback-on-error / no-commit contract is otherwise 1:1.
    """
    session = Mock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    class Command:
        def __init__(self) -> None:
            self.session = session

        @decorators.transaction()
        async def run(self) -> None:
            raise ValueError("error")

    with pytest.raises(ValueError, match="error"):
        await Command().run()
    session.commit.assert_not_called()
    session.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_transacation_nested() -> None:
    """
    Test the `transaction` decorator when the function is nested.

    Ported to the Liteset per-instance ``AsyncSession`` contract; the inner
    (re-entrant) call must not commit, and the outer rollback fires once.
    Re-entrancy is tracked on a ContextVar instead of ``g.in_transaction``.
    """
    session = Mock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    class Command:
        def __init__(self) -> None:
            self.session = session

        @decorators.transaction()
        async def func(self) -> int:
            return 42

        @decorators.transaction()
        async def nested(self) -> int:
            await self.func()  # should not commit
            raise ValueError("error")

    with pytest.raises(ValueError, match="error"):
        await Command().nested()
    session.commit.assert_not_called()
    session.rollback.assert_called_once()
