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
"""Flask-free port of ``tests/integration_tests/test_jinja_context.py``.

The upstream ``app_context`` fixture is dropped; instead ``ENABLE_TEMPLATE_
PROCESSING`` is forced on by patching the ``feature_flag_manager`` the
``jinja_context`` module reads, and the example database is fetched through the
real sync helper against the seeded Postgres backend.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

import superset.utils.database
from superset.exceptions import SupersetTemplateException
from superset.jinja_context import (
    get_template_processor,
    PrestoTemplateProcessor,
)


@pytest.fixture(autouse=True)
def _backend(integration_backend: str) -> None:
    """Ensure the schema + example data are present before these DB-backed tests.

    Several tests resolve the ``examples`` database via the sync helper
    ``get_example_database`` (which queries ``dbs``); without depending on
    ``integration_backend`` they could run before the session-scoped migrate,
    hitting ``relation "dbs" does not exist`` and poisoning the shared sync
    session for later modules.
    """


@pytest.fixture(autouse=True)
def _enable_template_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``ENABLE_TEMPLATE_PROCESSING`` on for every test.

    Upstream relies on the test config enabling this flag; here we patch the
    ``feature_flag_manager`` singleton that ``jinja_context`` consults so the
    real Jinja processors (not the no-op one) are selected.
    """
    monkeypatch.setattr(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(
            is_feature_enabled=lambda feature: feature == "ENABLE_TEMPLATE_PROCESSING"
        ),
    )


# ---------------------------------------------------------------------------
# Custom template processor used by the "$DATE()" macro tests. Inlined from the
# upstream ``tests/integration_tests/superset_test_custom_template_processors``
# module so we don't import the Flask-based integration test package.
# ---------------------------------------------------------------------------


def DATE(  # noqa: N802
    ts: datetime, day_offset: Any = 0, hour_offset: Any = 0
) -> str:
    """Current day as a string."""
    day_offset, hour_offset = int(day_offset), int(hour_offset)
    offset_day = (ts + timedelta(days=day_offset, hours=hour_offset)).date()
    return str(offset_day)


class CustomPrestoTemplateProcessor(PrestoTemplateProcessor):
    """A custom presto template processor for test."""

    engine = "db_for_macros_testing"

    def process_template(self, sql: str, **kwargs: Any) -> str:
        """Processes a sql template with $ style macro using regex."""
        macros = {"DATE": partial(DATE, datetime.utcnow())}
        macros.update(self._context)
        macros.update(kwargs)

        def replacer(match: re.Match[str]) -> str:
            macro_name, args_str = match.groups()
            args = [a.strip() for a in args_str.split(",")]
            if args == [""]:
                args = []
            f = macros[macro_name[1:]]
            return f(*args)

        macro_names = ["$" + name for name in macros.keys()]
        pattern = r"(%s)\s*\(([^()]*)\)" % "|".join(map(re.escape, macro_names))
        return re.sub(pattern, replacer, sql)


@pytest.fixture
def _register_custom_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the custom processor for ``db_for_macros_testing`` backend.

    Upstream wires this through ``CUSTOM_TEMPLATE_PROCESSORS`` config; here we
    patch the (lru-cached) ``get_template_processors`` lookup directly so the
    custom class is selected for the test backend while keeping the defaults.
    """
    from superset.jinja_context import DEFAULT_PROCESSORS

    processors = {
        **DEFAULT_PROCESSORS,
        "db_for_macros_testing": CustomPrestoTemplateProcessor,
    }
    monkeypatch.setattr(
        "superset.jinja_context.get_template_processors",
        lambda: processors,
    )


def test_process_template() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "SELECT '{{ 1+1 }}'"
    tp = get_template_processor(database=maindb)
    assert tp.process_template(template) == "SELECT '2'"


def test_get_template_kwarg() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ foo }}"
    tp = get_template_processor(database=maindb, foo="bar")
    assert tp.process_template(template) == "bar"


def test_template_kwarg() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ foo }}"
    tp = get_template_processor(database=maindb)
    assert tp.process_template(template, foo="bar") == "bar"


def test_get_template_kwarg_dict() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ foo.bar }}"
    tp = get_template_processor(database=maindb, foo={"bar": "baz"})
    assert tp.process_template(template) == "baz"


def test_template_kwarg_dict() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ foo.bar }}"
    tp = get_template_processor(database=maindb)
    assert tp.process_template(template, foo={"bar": "baz"}) == "baz"


def test_get_template_kwarg_lambda() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ foo() }}"
    tp = get_template_processor(database=maindb, foo=lambda: "bar")
    with pytest.raises(SupersetTemplateException):
        tp.process_template(template)


def test_template_kwarg_lambda() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ foo() }}"
    tp = get_template_processor(database=maindb)
    with pytest.raises(SupersetTemplateException):
        tp.process_template(template, foo=lambda: "bar")


def test_get_template_kwarg_module() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ dt(2017, 1, 1).isoformat() }}"
    tp = get_template_processor(database=maindb, dt=datetime)
    with pytest.raises(SupersetTemplateException):
        tp.process_template(template)


def test_template_kwarg_module() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ dt(2017, 1, 1).isoformat() }}"
    tp = get_template_processor(database=maindb)
    with pytest.raises(SupersetTemplateException):
        tp.process_template(template, dt=datetime)


def test_get_template_kwarg_nested_module() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ foo.dt }}"
    tp = get_template_processor(database=maindb, foo={"dt": datetime})
    with pytest.raises(SupersetTemplateException):
        tp.process_template(template)


def test_template_kwarg_nested_module() -> None:
    maindb = superset.utils.database.get_example_database()
    template = "{{ foo.dt }}"
    tp = get_template_processor(database=maindb)
    with pytest.raises(SupersetTemplateException):
        tp.process_template(template, foo={"bar": datetime})


def test_template_hive(mocker: MockerFixture) -> None:
    lp_mock = mocker.patch(
        "superset.jinja_context.HiveTemplateProcessor.latest_partition"
    )
    lp_mock.return_value = "the_latest"
    database = mock.Mock()
    database.backend = "hive"
    template = "{{ hive.latest_partition('my_table') }}"
    tp = get_template_processor(database=database)
    assert tp.process_template(template) == "the_latest"


def test_template_spark(mocker: MockerFixture) -> None:
    lp_mock = mocker.patch(
        "superset.jinja_context.SparkTemplateProcessor.latest_partition"
    )
    lp_mock.return_value = "the_latest"
    database = mock.Mock()
    database.backend = "spark"
    template = "{{ spark.latest_partition('my_table') }}"
    tp = get_template_processor(database=database)
    assert tp.process_template(template) == "the_latest"

    # Backwards compatibility if migrating from Hive.
    template = "{{ hive.latest_partition('my_table') }}"
    tp = get_template_processor(database=database)
    assert tp.process_template(template) == "the_latest"


def test_template_trino(mocker: MockerFixture) -> None:
    lp_mock = mocker.patch(
        "superset.jinja_context.TrinoTemplateProcessor.latest_partition"
    )
    lp_mock.return_value = "the_latest"
    database = mock.Mock()
    database.backend = "trino"
    template = "{{ trino.latest_partition('my_table') }}"
    tp = get_template_processor(database=database)
    assert tp.process_template(template) == "the_latest"

    # Backwards compatibility if migrating from Presto.
    template = "{{ presto.latest_partition('my_table') }}"
    tp = get_template_processor(database=database)
    assert tp.process_template(template) == "the_latest"


def test_template_context_addons(mocker: MockerFixture) -> None:
    addons_mock = mocker.patch("superset.jinja_context.context_addons")
    addons_mock.return_value = {"datetime": datetime}
    maindb = superset.utils.database.get_example_database()
    template = "SELECT '{{ datetime(2017, 1, 1).isoformat() }}'"
    tp = get_template_processor(database=maindb)
    assert tp.process_template(template) == "SELECT '2017-01-01T00:00:00'"


@pytest.mark.usefixtures("_register_custom_processor")
def test_custom_process_template(mocker: MockerFixture) -> None:
    """Test macro defined in custom template processor works."""
    # Keep a real reference for building the expected ``utcnow`` value before
    # patching the module-global ``datetime`` that ``DATE`` reads.
    real_datetime = datetime
    mock_dt = mocker.patch(
        f"{__name__}.datetime",
    )
    mock_dt.utcnow = mock.Mock(return_value=real_datetime(1970, 1, 1))
    database = mock.Mock()
    database.backend = "db_for_macros_testing"
    tp = get_template_processor(database=database)

    template = "SELECT '$DATE()'"
    assert tp.process_template(template) == "SELECT '1970-01-01'"

    template = "SELECT '$DATE(1, 2)'"
    assert tp.process_template(template) == "SELECT '1970-01-02'"


@pytest.mark.usefixtures("_register_custom_processor")
def test_custom_get_template_kwarg() -> None:
    """Test macro passed as kwargs when getting template processor
    works in custom template processor."""
    database = mock.Mock()
    database.backend = "db_for_macros_testing"
    template = "$foo()"
    tp = get_template_processor(database=database, foo=lambda: "bar")
    assert tp.process_template(template) == "bar"


@pytest.mark.usefixtures("_register_custom_processor")
def test_custom_template_kwarg() -> None:
    """Test macro passed as kwargs when processing template
    works in custom template processor."""
    database = mock.Mock()
    database.backend = "db_for_macros_testing"
    template = "$foo()"
    tp = get_template_processor(database=database)
    assert tp.process_template(template, foo=lambda: "bar") == "bar"


@pytest.mark.usefixtures("_register_custom_processor")
def test_custom_template_processors_overwrite() -> None:
    """Test template processor for presto gets overwritten by custom one."""
    database = mock.Mock()
    database.backend = "db_for_macros_testing"
    tp = get_template_processor(database=database)

    template = "SELECT '{{ datetime(2017, 1, 1).isoformat() }}'"
    assert tp.process_template(template) == template

    template = "SELECT '{{ DATE(1, 2) }}'"
    assert tp.process_template(template) == template


def test_custom_template_processors_ignored() -> None:
    """Test custom template processor is ignored for a difference backend
    database."""
    maindb = superset.utils.database.get_example_database()
    template = "SELECT '$DATE()'"
    tp = get_template_processor(database=maindb)
    assert tp.process_template(template) == template
