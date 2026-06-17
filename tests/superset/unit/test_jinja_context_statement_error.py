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
"""Regression tests: StatementError guard in _sync_find_dataset and
_dataset_id_from_chart.

``BaseDAO.find_by_id`` wraps ``filter().one_or_none()`` in
``except StatementError: return None`` so that a non-numeric or wrong-type ID
string (e.g. ``"bad"`` from a malformed ``datasource_info.split("__")[0]``)
returns None → DatasetNotFoundError (404) rather than propagating a DB
error → 500.

Liteset's _sync_find_dataset must mirror that guard.
_dataset_id_from_chart must also convert ValueError (from int()) and StatementError
into SupersetTemplateException instead of a raw uncaught exception.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import StatementError

from superset.exceptions import SupersetTemplateException
from superset.jinja_context import _dataset_id_from_chart, _sync_find_dataset


def _make_session_raises(exc: Exception) -> MagicMock:
    session = MagicMock()
    session.execute.side_effect = exc
    # Context-manager protocol used by ``with Session(engine) as session``
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    return session


def _make_session_returns(value: object) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.one_or_none.return_value = value
    session = MagicMock()
    session.execute.return_value = result
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    return session


@contextmanager
def _patched_session(session_mock: MagicMock):
    """Context manager that patches sqlalchemy.orm.Session to return
    *session_mock* from its constructor.

    The Session import in _sync_find_dataset / _dataset_id_from_chart is a
    local ``from sqlalchemy.orm import Session`` inside the function body.
    Patching ``sqlalchemy.orm.Session`` intercepts all uses inside those
    functions.
    """
    with patch("sqlalchemy.orm.Session", return_value=session_mock):
        yield


def test_sync_find_dataset_returns_none_on_statement_error():
    """StatementError from the DB must be caught and None returned (mirrors
    BaseDAO.find_by_id's guard) so callers raise DatasetNotFoundError (404)
    rather than propagating a 500."""
    exc = StatementError("invalid input", None, None, None)
    session_mock = _make_session_raises(exc)
    engine_mock = MagicMock()

    with patch("superset.jinja_context._get_sync_engine", return_value=engine_mock):
        with _patched_session(session_mock):
            result = _sync_find_dataset("bad")  # type: ignore[arg-type]

    assert result is None


def test_sync_find_dataset_returns_none_on_statement_error_numeric_string():
    """Even a numeric-ish string ID that somehow causes a StatementError must
    return None, not raise."""
    exc = StatementError("type error", None, None, None)
    session_mock = _make_session_raises(exc)
    engine_mock = MagicMock()

    with patch("superset.jinja_context._get_sync_engine", return_value=engine_mock):
        with _patched_session(session_mock):
            result = _sync_find_dataset(5)

    assert result is None


def test_sync_find_dataset_returns_dataset_on_success():
    fake_dataset = MagicMock()
    session_mock = _make_session_returns(fake_dataset)
    engine_mock = MagicMock()

    with patch("superset.jinja_context._get_sync_engine", return_value=engine_mock):
        with _patched_session(session_mock):
            result = _sync_find_dataset(42)

    assert result is fake_dataset


def test_sync_find_dataset_returns_none_when_not_found():
    """one_or_none() returning None is forwarded as None (dataset does not
    exist)."""
    session_mock = _make_session_returns(None)
    engine_mock = MagicMock()

    with patch("superset.jinja_context._get_sync_engine", return_value=engine_mock):
        with _patched_session(session_mock):
            result = _sync_find_dataset(999)

    assert result is None


_EXC_MSG = "Please specify the Dataset ID"


def test_dataset_id_from_chart_raises_on_non_numeric_chart_id():
    """Non-numeric chart_id must raise SupersetTemplateException (mirrors
    ChartDAO.find_by_id catching StatementError → None → raise).  Previously
    int() raised ValueError which propagated as an uncaught 500."""
    engine_mock = MagicMock()
    with patch("superset.jinja_context._get_sync_engine", return_value=engine_mock):
        with pytest.raises(SupersetTemplateException):
            _dataset_id_from_chart("bad_chart_id", _EXC_MSG)


def test_dataset_id_from_chart_raises_on_statement_error():
    """StatementError from the DB during chart lookup must raise
    SupersetTemplateException, not propagate as a 500."""
    exc = StatementError("invalid input", None, None, None)
    session_mock = _make_session_raises(exc)
    engine_mock = MagicMock()

    with patch("superset.jinja_context._get_sync_engine", return_value=engine_mock):
        with _patched_session(session_mock):
            with pytest.raises(SupersetTemplateException):
                _dataset_id_from_chart(5, _EXC_MSG)


def test_dataset_id_from_chart_raises_when_chart_not_found():
    session_mock = _make_session_returns(None)
    engine_mock = MagicMock()

    with patch("superset.jinja_context._get_sync_engine", return_value=engine_mock):
        with _patched_session(session_mock):
            with pytest.raises(SupersetTemplateException):
                _dataset_id_from_chart(999, _EXC_MSG)


def test_dataset_id_from_chart_returns_datasource_id_on_success():
    fake_chart = MagicMock()
    fake_chart.datasource_id = 7
    session_mock = _make_session_returns(fake_chart)
    engine_mock = MagicMock()

    with patch("superset.jinja_context._get_sync_engine", return_value=engine_mock):
        with _patched_session(session_mock):
            result = _dataset_id_from_chart(42, _EXC_MSG)

    assert result == 7
