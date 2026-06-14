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
"""Unit tests for ``superset.jinja_context`` (Flask-free port).

Adapted 1:1 in intent from ``tests/unit_tests/jinja_context_test.py``.

Liteset adaptations vs the upstream Flask suite:
  * ``current_app.test_request_context(data=/query_string=/json=...)`` is
    replaced by ``set_form_data()`` (the request-scoped ``_form_data_ctx``
    ContextVar that the request middleware fills) plus, for ``url_param``,
    a mock request bound through ``set_current_request()``.
  * ``g.user`` is replaced by ``set_current_user()`` (the
    ``_current_user_ctx`` ContextVar).
  * The synchronous template path does not use the async ``DatasetDAO``; it
    calls the module-level ``_sync_find_dataset`` and
    ``_sync_user_can_access_dataset`` helpers, which the tests patch instead
    of ``DatasetDAO.find_by_id``. ``skip_base_filter`` is asserted on
    ``_sync_user_can_access_dataset`` rather than on ``find_by_id``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time
from jinja2 import DebugUndefined
from jinja2.sandbox import SandboxedEnvironment
from pytest_mock import MockerFixture
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.postgresql import dialect

from superset.exceptions import DatasetNotFoundError, SupersetTemplateException
from superset.jinja_context import (
    dataset_macro,
    ExtraCache,
    get_template_processor,
    metric_macro,
    safe_proxy,
    set_form_data,
    TimeFilter,
    to_datetime,
    WhereInMacro,
)
from superset.models.connectors import SqlaTable, SqlMetric, TableColumn
from superset.models.core import Database
from superset.utils import json
from superset.utils.core import set_current_request, set_current_user


@pytest.fixture(autouse=True)
def _reset_context() -> Iterator[None]:
    """Reset the request-scoped context vars between tests."""
    set_form_data({})
    set_current_request(None)
    set_current_user(None)
    yield
    set_form_data({})
    set_current_request(None)
    set_current_user(None)


def _mock_request(query_params: dict[str, Any] | None = None) -> MagicMock:
    request = MagicMock()
    request.query_params = query_params or {}
    return request


def test_filter_values_adhoc_filters() -> None:
    """
    Test the ``filter_values`` macro with ``adhoc_filters``.
    """
    set_form_data(
        {
            "adhoc_filters": [
                {
                    "clause": "WHERE",
                    "comparator": "foo",
                    "expressionType": "SIMPLE",
                    "operator": "in",
                    "subject": "name",
                }
            ],
        }
    )
    cache = ExtraCache()
    assert cache.filter_values("name") == ["foo"]
    assert cache.applied_filters == ["name"]

    set_form_data(
        {
            "adhoc_filters": [
                {
                    "clause": "WHERE",
                    "comparator": ["foo", "bar"],
                    "expressionType": "SIMPLE",
                    "operator": "in",
                    "subject": "name",
                }
            ],
        }
    )
    cache = ExtraCache()
    assert cache.filter_values("name") == ["foo", "bar"]
    assert cache.applied_filters == ["name"]


def test_filter_values_extra_filters() -> None:
    """
    Test the ``filter_values`` macro with ``extra_filters``.
    """
    set_form_data({"extra_filters": [{"col": "name", "op": "in", "val": "foo"}]})
    cache = ExtraCache()
    assert cache.filter_values("name") == ["foo"]
    assert cache.applied_filters == ["name"]


def test_filter_values_default() -> None:
    """
    Test the ``filter_values`` macro with a default value.
    """
    cache = ExtraCache()
    assert cache.filter_values("name", "foo") == ["foo"]
    assert cache.removed_filters == []


def test_filter_values_remove_not_present() -> None:
    """
    Test the ``filter_values`` macro without a match and ``remove_filter`` set to True.
    """
    cache = ExtraCache()
    assert cache.filter_values("name", remove_filter=True) == []
    assert cache.removed_filters == []


def test_filter_values_no_default() -> None:
    """
    Test calling the ``filter_values`` macro without a match.
    """
    cache = ExtraCache()
    assert cache.filter_values("name") == []


def test_get_filters_adhoc_filters() -> None:
    """
    Test the ``get_filters`` macro.
    """
    set_form_data(
        {
            "adhoc_filters": [
                {
                    "clause": "WHERE",
                    "comparator": "foo",
                    "expressionType": "SIMPLE",
                    "operator": "in",
                    "subject": "name",
                }
            ],
        }
    )
    cache = ExtraCache()
    assert cache.get_filters("name") == [{"op": "IN", "col": "name", "val": ["foo"]}]
    assert cache.removed_filters == []
    assert cache.applied_filters == ["name"]

    set_form_data(
        {
            "adhoc_filters": [
                {
                    "clause": "WHERE",
                    "comparator": ["foo", "bar"],
                    "expressionType": "SIMPLE",
                    "operator": "in",
                    "subject": "name",
                }
            ],
        }
    )
    cache = ExtraCache()
    assert cache.get_filters("name") == [
        {"op": "IN", "col": "name", "val": ["foo", "bar"]}
    ]
    assert cache.removed_filters == []

    set_form_data(
        {
            "adhoc_filters": [
                {
                    "clause": "WHERE",
                    "comparator": ["foo", "bar"],
                    "expressionType": "SIMPLE",
                    "operator": "in",
                    "subject": "name",
                }
            ],
        }
    )
    cache = ExtraCache()
    assert cache.get_filters("name", remove_filter=True) == [
        {"op": "IN", "col": "name", "val": ["foo", "bar"]}
    ]
    assert cache.removed_filters == ["name"]
    assert cache.applied_filters == ["name"]


def test_get_filters_is_null_operator() -> None:
    """
    Test the ``get_filters`` macro with a IS_NULL operator,
    which doesn't have a comparator
    """
    set_form_data(
        {
            "adhoc_filters": [
                {
                    "clause": "WHERE",
                    "expressionType": "SIMPLE",
                    "operator": "IS NULL",
                    "subject": "name",
                    "comparator": None,
                }
            ],
        }
    )
    cache = ExtraCache()
    assert cache.get_filters("name", remove_filter=True) == [
        {"op": "IS NULL", "col": "name", "val": None}
    ]
    assert cache.removed_filters == ["name"]
    assert cache.applied_filters == ["name"]


def test_get_filters_remove_not_present() -> None:
    """
    Test the ``get_filters`` macro without a match and ``remove_filter`` set to True.
    """
    cache = ExtraCache()
    assert cache.get_filters("name", remove_filter=True) == []
    assert cache.removed_filters == []


def test_url_param_query() -> None:
    """
    Test the ``url_param`` macro.
    """
    set_current_request(_mock_request({"foo": "bar"}))
    cache = ExtraCache()
    assert cache.url_param("foo") == "bar"


def test_url_param_default() -> None:
    """
    Test the ``url_param`` macro with a default value.
    """
    cache = ExtraCache()
    assert cache.url_param("foo", "bar") == "bar"


def test_url_param_no_default() -> None:
    """
    Test the ``url_param`` macro without a match.
    """
    cache = ExtraCache()
    assert cache.url_param("foo") is None


def test_url_param_form_data() -> None:
    """
    Test the ``url_param`` with ``url_params`` in ``form_data``.
    """
    set_form_data({"url_params": {"foo": "bar"}})
    cache = ExtraCache()
    assert cache.url_param("foo") == "bar"


def test_url_param_escaped_form_data() -> None:
    """
    Test the ``url_param`` with ``url_params`` in ``form_data`` returning
    an escaped value with a quote.
    """
    set_form_data({"url_params": {"foo": "O'Brien"}})
    cache = ExtraCache(dialect=dialect())
    assert cache.url_param("foo") == "O''Brien"


def test_url_param_escaped_default_form_data() -> None:
    """
    Test the ``url_param`` with default value containing an escaped quote.
    """
    set_form_data({"url_params": {"foo": "O'Brien"}})
    cache = ExtraCache(dialect=dialect())
    assert cache.url_param("bar", "O'Malley") == "O''Malley"


def test_url_param_unescaped_form_data() -> None:
    """
    Test the ``url_param`` with ``url_params`` in ``form_data`` returning
    an un-escaped value with a quote.
    """
    set_form_data({"url_params": {"foo": "O'Brien"}})
    cache = ExtraCache(dialect=dialect())
    assert cache.url_param("foo", escape_result=False) == "O'Brien"


def test_url_param_unescaped_default_form_data() -> None:
    """
    Test the ``url_param`` with default value containing an un-escaped quote.
    """
    set_form_data({"url_params": {"foo": "O'Brien"}})
    cache = ExtraCache(dialect=dialect())
    assert cache.url_param("bar", "O'Malley", escape_result=False) == "O'Malley"


def test_safe_proxy_primitive() -> None:
    """
    Test the ``safe_proxy`` helper with a function returning a ``str``.
    """

    def func(input_: Any) -> Any:
        return input_

    assert safe_proxy(func, "foo") == "foo"


def test_safe_proxy_dict() -> None:
    """
    Test the ``safe_proxy`` helper with a function returning a ``dict``.
    """

    def func(input_: Any) -> Any:
        return input_

    assert safe_proxy(func, {"foo": "bar"}) == {"foo": "bar"}


def test_safe_proxy_lambda() -> None:
    """
    Test the ``safe_proxy`` helper with a function returning a ``lambda``.
    Should raise ``SupersetTemplateException``.
    """

    def func(input_: Any) -> Any:
        return input_

    with pytest.raises(SupersetTemplateException):
        safe_proxy(func, lambda: "bar")


def test_safe_proxy_nested_lambda() -> None:
    """
    Test the ``safe_proxy`` helper with a function returning a ``dict``
    containing ``lambda`` value. Should raise ``SupersetTemplateException``.
    """

    def func(input_: Any) -> Any:
        return input_

    with pytest.raises(SupersetTemplateException):
        safe_proxy(func, {"foo": lambda: "bar"})


@pytest.mark.parametrize(
    "add_to_cache_keys,mock_cache_key_wrapper_call_count",
    [
        (True, 4),
        (False, 0),
    ],
)
def test_user_macros(
    mocker: MockerFixture,
    add_to_cache_keys: bool,
    mock_cache_key_wrapper_call_count: int,
):
    """
    Test all user macros:
        - ``current_user_id``
        - ``current_username``
        - ``current_user_email``
        - ``current_user_roles``
        - ``current_user_rls_rules``
    """
    user = SimpleNamespace(
        id=1,
        username="my_username",
        email="my_email@test.com",
        roles=[
            SimpleNamespace(id=1, name="my_role1"),
            SimpleNamespace(id=2, name="my_role2"),
        ],
    )
    set_current_user(user)
    mocker.patch(
        "superset.jinja_context._sync_get_user_group_role_names",
        return_value=[],
    )
    mocker.patch(
        "superset.jinja_context._sync_get_rls_rules",
        return_value=["1=1", "product_id=1"],
    )
    mock_cache_key_wrapper = mocker.patch(
        "superset.jinja_context.ExtraCache.cache_key_wrapper"
    )
    cache = ExtraCache(table=mocker.MagicMock())
    assert cache.current_user_id(add_to_cache_keys) == 1
    assert cache.current_username(add_to_cache_keys) == "my_username"
    assert cache.current_user_email(add_to_cache_keys) == "my_email@test.com"
    assert cache.current_user_roles(add_to_cache_keys) == ["my_role1", "my_role2"]
    assert mock_cache_key_wrapper.call_count == mock_cache_key_wrapper_call_count

    # Testing {{ current_user_rls_rules() }} macro isolated and always without
    # the param because it does not support it to avoid shared cache.
    assert cache.current_user_rls_rules() == ["1=1", "product_id=1"]


def test_user_macros_without_user_info(mocker: MockerFixture):
    """
    Test all user macros when no user info is available.
    """
    set_current_user(None)
    cache = ExtraCache(table=mocker.MagicMock())
    assert cache.current_user_id() is None
    assert cache.current_username() is None
    assert cache.current_user_email() is None
    assert cache.current_user_roles() is None
    assert cache.current_user_rls_rules() is None


def test_current_user_rls_rules_with_no_table(mocker: MockerFixture):
    """
    Test the ``current_user_rls_rules`` macro when no table is provided.
    """
    user = SimpleNamespace(
        id=1,
        username="my_username",
        email="my_email@test.com",
        roles=[],
    )
    set_current_user(user)
    mock_get_user_rls = mocker.patch("superset.jinja_context._sync_get_rls_rules")
    mock_cache_key_wrapper = mocker.patch(
        "superset.jinja_context.ExtraCache.cache_key_wrapper"
    )
    cache = ExtraCache()
    assert cache.current_user_rls_rules() is None
    assert mock_cache_key_wrapper.call_count == 0
    assert mock_get_user_rls.call_count == 0


def test_current_user_rls_rules_guest_user(mocker: MockerFixture):
    """
    Test the ``current_user_rls_rules`` with an embedded user.
    """
    mocker.patch(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=lambda feature: feature == "EMBEDDED_SUPERSET"),
    )
    user = SimpleNamespace(
        username="my_username",
        is_guest=True,
        is_anonymous=False,
        rls_rules=[
            {"group_key": "test", "clause": "1=1"},
            {"group_key": "other_test", "clause": "product_id=1"},
        ],
    )
    set_current_user(user)
    cache = ExtraCache(table=SimpleNamespace(id=1))
    assert cache.current_user_rls_rules() == ["1=1", "product_id=1"]


def test_where_in() -> None:
    """
    Test the ``where_in`` Jinja2 filter.
    """
    where_in = WhereInMacro(mysql.dialect())
    assert where_in([1, "b", 3]) == "(1, 'b', 3)"
    assert where_in([1, "b", 3], '"') == (
        "(1, 'b', 3)\n-- WARNING: the `mark` parameter was removed from the "
        "`where_in` macro for security reasons\n"
    )
    assert where_in(["O'Malley's"]) == "('O''Malley''s')"


def test_where_in_empty_list() -> None:
    """
    Test the ``where_in`` Jinja2 filter when it receives an
    empty list.
    """
    where_in = WhereInMacro(mysql.dialect())

    # By default, the filter should return empty parenthesis (as a string)
    assert where_in([]) == "()"
    # With the default_to_none parameter set to True, it should return None
    assert where_in([], default_to_none=True) is None


@pytest.mark.parametrize(
    "value,format,output",
    [
        ("2025-03-20 15:55:00", None, datetime(2025, 3, 20, 15, 55)),
        (None, None, None),
        ("2025-03-20", "%Y-%m-%d", datetime(2025, 3, 20)),
        ("'2025-03-20'", "%Y-%m-%d", datetime(2025, 3, 20)),
    ],
)
def test_to_datetime(
    value: str | None, format: str | None, output: datetime | None
) -> None:
    """
    Test the ``to_datetime`` custom filter.
    """
    result = (
        to_datetime(value, format=format) if format is not None else to_datetime(value)
    )
    assert result == output


@pytest.mark.parametrize(
    "value,format,match",
    [
        (
            "2025-03-20",
            None,
            "time data '2025-03-20' does not match format '%Y-%m-%d %H:%M:%S'",
        ),
        (
            "2025-03-20 15:55:00",
            "%Y-%m-%d",
            "unconverted data remains:  15:55:00",
        ),
    ],
)
def test_to_datetime_raises(value: str, format: str | None, match: str) -> None:
    """
    Test the ``to_datetime`` custom filter raises with an incorrect
    format.
    """
    with pytest.raises(
        ValueError,
        match=match,
    ):
        (
            to_datetime(value, format=format)
            if format is not None
            else to_datetime(value)
        )


def test_dataset_macro(mocker: MockerFixture) -> None:
    """
    Test the ``dataset_macro`` macro.
    """
    columns = [
        TableColumn(column_name="ds", is_dttm=1, type="TIMESTAMP"),
        TableColumn(column_name="num_boys", type="INTEGER"),
        TableColumn(column_name="revenue", type="INTEGER"),
        TableColumn(column_name="expenses", type="INTEGER"),
        TableColumn(
            column_name="profit", type="INTEGER", expression="revenue-expenses"
        ),
    ]
    metrics = [
        SqlMetric(metric_name="cnt", expression="COUNT(*)"),
    ]

    dataset = SqlaTable(
        table_name="old_dataset",
        columns=columns,
        metrics=metrics,
        main_dttm_col="ds",
        default_endpoint="https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # not used
        database=Database(database_name="my_database", sqlalchemy_uri="sqlite://"),
        offset=-8,
        description="This is the description",
        is_featured=1,
        cache_timeout=3600,
        schema="my_schema",
        sql=None,
        params=json.dumps(
            {
                "remote_id": 64,
                "database_name": "examples",
                "import_time": 1606677834,
            }
        ),
        perm=None,
        filter_select_enabled=1,
        fetch_values_predicate="foo IN (1, 2)",
        is_sqllab_view=0,  # no longer used?
        template_params=json.dumps({"answer": "42"}),
        schema_perm=None,
        extra=json.dumps({"warning_markdown": "*WARNING*"}),
    )
    find_dataset = mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    space = " "

    assert (
        dataset_macro(1)
        == f"""(
SELECT ds AS ds, num_boys AS num_boys, revenue AS revenue, expenses AS expenses, revenue-expenses AS profit{space}
FROM my_schema.old_dataset
) AS dataset_1"""  # noqa: S608, E501
    )

    assert (
        dataset_macro(1, include_metrics=True)
        == f"""(
SELECT ds AS ds, num_boys AS num_boys, revenue AS revenue, expenses AS expenses, revenue-expenses AS profit, COUNT(*) AS cnt{space}
FROM my_schema.old_dataset GROUP BY ds, num_boys, revenue, expenses, revenue-expenses
) AS dataset_1"""  # noqa: S608, E501
    )

    assert (
        dataset_macro(1, include_metrics=True, columns=["ds"])
        == f"""(
SELECT ds AS ds, COUNT(*) AS cnt{space}
FROM my_schema.old_dataset GROUP BY ds
) AS dataset_1"""  # noqa: S608
    )

    find_dataset.return_value = None
    with pytest.raises(DatasetNotFoundError) as excinfo:
        dataset_macro(1)
    assert str(excinfo.value) == "Dataset 1 not found!"


def test_dataset_macro_mutator_with_comments(mocker: MockerFixture) -> None:
    """
    Test ``dataset_macro`` when the mutator adds comment.
    """

    def mutator(sql: str) -> str:
        """
        A simple mutator that wraps the query in comments.
        """
        return f"-- begin\n{sql}\n-- end"

    dataset = mocker.MagicMock()
    dataset.columns = []
    dataset.metrics = []
    dataset.get_query_str_extended.return_value.sql = mutator("SELECT 1")
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )
    assert (
        dataset_macro(1)
        == """(
-- begin
SELECT 1
-- end
) AS dataset_1"""
    )


def test_metric_macro_with_dataset_id(mocker: MockerFixture) -> None:
    """
    Test the ``metric_macro`` when passing a dataset ID.
    """
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=SqlaTable(
            table_name="test_dataset",
            metrics=[
                SqlMetric(metric_name="count", expression="COUNT(*)"),
            ],
            database=Database(database_name="my_database", sqlalchemy_uri="sqlite://"),
            schema="my_schema",
            sql=None,
        ),
    )
    get_dataset_id_from_context = mocker.patch(
        "superset.jinja_context.get_dataset_id_from_context"
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )
    env = SandboxedEnvironment(undefined=DebugUndefined)
    assert metric_macro(env, {}, "count", 1) == "COUNT(*)"
    get_dataset_id_from_context.assert_not_called()


def test_metric_macro_recursive(mocker: MockerFixture) -> None:
    """
    Test the ``metric_macro`` when the definition is recursive.
    """
    database = Database(id=1, database_name="my_database", sqlalchemy_uri="sqlite://")
    dataset = SqlaTable(
        id=1,
        metrics=[
            SqlMetric(metric_name="a", expression="COUNT(*)"),
            SqlMetric(metric_name="b", expression="{{ metric('a') }}"),
            SqlMetric(metric_name="c", expression="{{ metric('b') }}"),
        ],
        table_name="test_dataset",
        database=database,
        schema="my_schema",
        sql=None,
    )

    set_form_data({"datasource": {"id": 1}})
    mocker.patch(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(
            is_feature_enabled=lambda feature: feature == "ENABLE_TEMPLATE_PROCESSING"
        ),
    )
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    processor = get_template_processor(database=database)
    assert processor.process_template("{{ metric('c', 1) }}") == "COUNT(*)"


def test_metric_macro_expansion(mocker: MockerFixture) -> None:
    """
    Test that the ``metric_macro`` expands other macros.
    """
    database = Database(id=1, database_name="my_database", sqlalchemy_uri="sqlite://")
    dataset = SqlaTable(
        id=1,
        metrics=[
            SqlMetric(metric_name="a", expression="{{ current_user_id() }}"),
            SqlMetric(metric_name="b", expression="{{ metric('a') }}"),
            SqlMetric(metric_name="c", expression="{{ metric('b') }}"),
        ],
        table_name="test_dataset",
        database=database,
        schema="my_schema",
        sql=None,
    )

    mocker.patch("superset.jinja_context.get_user_id", return_value=42)
    set_form_data({"datasource": {"id": 1}})
    mocker.patch(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(
            is_feature_enabled=lambda feature: feature == "ENABLE_TEMPLATE_PROCESSING"
        ),
    )
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    processor = get_template_processor(database=database)
    assert processor.process_template("{{ metric('c') }}") == "42"


def test_metric_macro_recursive_compound(mocker: MockerFixture) -> None:
    """
    Test the ``metric_macro`` when the definition is compound.
    """
    database = Database(id=1, database_name="my_database", sqlalchemy_uri="sqlite://")
    dataset = SqlaTable(
        id=1,
        metrics=[
            SqlMetric(metric_name="a", expression="SUM(*)"),
            SqlMetric(metric_name="b", expression="COUNT(*)"),
            SqlMetric(
                metric_name="c",
                expression="{{ metric('a') }} / {{ metric('b') }}",
            ),
        ],
        table_name="test_dataset",
        database=database,
        schema="my_schema",
        sql=None,
    )

    set_form_data({"datasource": {"id": 1}})
    mocker.patch(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(
            is_feature_enabled=lambda feature: feature == "ENABLE_TEMPLATE_PROCESSING"
        ),
    )
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    processor = get_template_processor(database=database)
    assert processor.process_template("{{ metric('c') }}") == "SUM(*) / COUNT(*)"


def test_metric_macro_recursive_cyclic(mocker: MockerFixture) -> None:
    """
    Test the ``metric_macro`` when the definition is cyclic.

    In this case it should stop, and not go into an infinite loop.
    """
    database = Database(id=1, database_name="my_database", sqlalchemy_uri="sqlite://")
    dataset = SqlaTable(
        id=1,
        metrics=[
            SqlMetric(metric_name="a", expression="{{ metric('c') }}"),
            SqlMetric(metric_name="b", expression="{{ metric('a') }}"),
            SqlMetric(metric_name="c", expression="{{ metric('b') }}"),
        ],
        table_name="test_dataset",
        database=database,
        schema="my_schema",
        sql=None,
    )

    set_form_data({"datasource": {"id": 1}})
    mocker.patch(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(
            is_feature_enabled=lambda feature: feature == "ENABLE_TEMPLATE_PROCESSING"
        ),
    )
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    processor = get_template_processor(database=database)
    with pytest.raises(SupersetTemplateException) as excinfo:
        processor.process_template("{{ metric('c') }}")
    assert str(excinfo.value) == "Infinite recursion detected in template"


def test_metric_macro_recursive_infinite(mocker: MockerFixture) -> None:
    """
    Test the ``metric_macro`` when the definition is cyclic.

    In this case it should stop, and not go into an infinite loop.
    """
    database = Database(id=1, database_name="my_database", sqlalchemy_uri="sqlite://")
    dataset = SqlaTable(
        id=1,
        metrics=[
            SqlMetric(metric_name="a", expression="{{ metric('a') }}"),
        ],
        table_name="test_dataset",
        database=database,
        schema="my_schema",
        sql=None,
    )

    set_form_data({"datasource": {"id": 1}})
    mocker.patch(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(
            is_feature_enabled=lambda feature: feature == "ENABLE_TEMPLATE_PROCESSING"
        ),
    )
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    processor = get_template_processor(database=database)
    with pytest.raises(SupersetTemplateException) as excinfo:
        processor.process_template("{{ metric('a') }}")
    assert str(excinfo.value) == "Infinite recursion detected in template"


def test_metric_macro_with_dataset_id_invalid_key(mocker: MockerFixture) -> None:
    """
    Test the ``metric_macro`` when passing a dataset ID and an invalid key.
    """
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=SqlaTable(
            table_name="test_dataset",
            metrics=[
                SqlMetric(metric_name="count", expression="COUNT(*)"),
            ],
            database=Database(database_name="my_database", sqlalchemy_uri="sqlite://"),
            schema="my_schema",
            sql=None,
        ),
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )
    get_dataset_id_from_context = mocker.patch(
        "superset.jinja_context.get_dataset_id_from_context"
    )
    env = SandboxedEnvironment(undefined=DebugUndefined)
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "blah", 1)
    assert str(excinfo.value) == "Metric ``blah`` not found in test_dataset."
    get_dataset_id_from_context.assert_not_called()


def test_metric_macro_invalid_dataset_id(mocker: MockerFixture) -> None:
    """
    Test the ``metric_macro`` when specifying a dataset that doesn't exist.
    """
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=None,
    )
    get_dataset_id_from_context = mocker.patch(
        "superset.jinja_context.get_dataset_id_from_context"
    )
    env = SandboxedEnvironment(undefined=DebugUndefined)
    with pytest.raises(DatasetNotFoundError) as excinfo:
        metric_macro(env, {}, "macro_key", 100)
    assert str(excinfo.value) == "Dataset ID 100 not found."
    get_dataset_id_from_context.assert_not_called()


def test_metric_macro_no_dataset_id_no_context(mocker: MockerFixture) -> None:
    """
    Test the ``metric_macro`` when not specifying a dataset ID and it's
    not available in the context.
    """
    find_dataset = mocker.patch("superset.jinja_context._sync_find_dataset")
    set_form_data({})
    env = SandboxedEnvironment(undefined=DebugUndefined)
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "macro_key")
    assert str(excinfo.value) == (
        "Please specify the Dataset ID for the ``macro_key`` metric in the Jinja macro."
    )
    find_dataset.assert_not_called()


def test_metric_macro_no_dataset_id_with_context_missing_info(
    mocker: MockerFixture,
) -> None:
    """
    Test the ``metric_macro`` when not specifying a dataset ID and request
    has context but no dataset/chart ID.
    """
    find_dataset = mocker.patch("superset.jinja_context._sync_find_dataset")
    # Context present (adhoc_filters) but no dataset/chart ID anywhere.
    set_form_data(
        {
            "adhoc_filters": [
                {
                    "clause": "WHERE",
                    "comparator": "foo",
                    "expressionType": "SIMPLE",
                    "operator": "in",
                    "subject": "name",
                }
            ],
        }
    )

    env = SandboxedEnvironment(undefined=DebugUndefined)
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "macro_key")
    assert str(excinfo.value) == (
        "Please specify the Dataset ID for the ``macro_key`` metric in the Jinja macro."
    )
    find_dataset.assert_not_called()


def test_metric_macro_no_dataset_id_with_context_empty_queries(
    mocker: MockerFixture,
) -> None:
    """An explicit empty ``queries`` list must not raise ``IndexError``.

    ``get_dataset_id_from_context`` reads ``queries[0]``; guard against the
    empty-list case so it surfaces the proper "specify the Dataset ID" error.
    """
    find_dataset = mocker.patch("superset.jinja_context._sync_find_dataset")
    set_form_data({"queries": []})

    env = SandboxedEnvironment(undefined=DebugUndefined)
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "macro_key")
    assert str(excinfo.value) == (
        "Please specify the Dataset ID for the ``macro_key`` metric in the Jinja macro."
    )
    find_dataset.assert_not_called()


def test_metric_macro_no_dataset_id_with_context_datasource_id(
    mocker: MockerFixture,
) -> None:
    """
    Test the ``metric_macro`` when not specifying a dataset ID and it's
    available in the context (url_params.datasource_id).
    """
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=SqlaTable(
            table_name="test_dataset",
            metrics=[
                SqlMetric(metric_name="macro_key", expression="COUNT(*)"),
            ],
            database=Database(database_name="my_database", sqlalchemy_uri="sqlite://"),
            schema="my_schema",
            sql=None,
        ),
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    env = SandboxedEnvironment(undefined=DebugUndefined)
    # Getting the data from the request context (the request body envelope).
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "datasource_id": 1,
                    }
                }
            ],
        }
    )
    assert metric_macro(env, {}, "macro_key") == "COUNT(*)"

    # Getting data from g's form_data. In the Liteset port both the request
    # body envelope and ``g.form_data`` resolve to the single ``_form_data_ctx``
    # ContextVar, so the second upstream scenario uses the same source.
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "datasource_id": 1,
                    }
                }
            ],
        }
    )
    assert metric_macro(env, {}, "macro_key") == "COUNT(*)"


def test_metric_macro_no_dataset_id_with_context_datasource_id_none(
    mocker: MockerFixture,
) -> None:
    """
    Test the ``metric_macro`` when not specifying a dataset ID and it's
    set to None in the context (url_params.datasource_id).
    """
    env = SandboxedEnvironment(undefined=DebugUndefined)
    # Getting the data from the request context (the request body envelope).
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "datasource_id": None,
                    }
                }
            ],
        }
    )
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "macro_key")
    assert str(excinfo.value) == (
        "Please specify the Dataset ID for the ``macro_key`` metric in the Jinja macro."
    )

    # Getting data from g's form_data. In the Liteset port both the request
    # body envelope and ``g.form_data`` resolve to the single ``_form_data_ctx``
    # ContextVar, so the second upstream scenario uses the same source.
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "datasource_id": None,
                    }
                }
            ],
        }
    )
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "macro_key")
    assert str(excinfo.value) == (
        "Please specify the Dataset ID for the ``macro_key`` metric in the Jinja macro."
    )


def test_metric_macro_no_dataset_id_with_context_chart_id(
    mocker: MockerFixture,
) -> None:
    """
    Test the ``metric_macro`` when not specifying a dataset ID and context
    includes an existing chart ID (url_params.slice_id).
    """
    mocker.patch(
        "superset.jinja_context._dataset_id_from_chart",
        return_value=1,
    )
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=SqlaTable(
            table_name="test_dataset",
            metrics=[
                SqlMetric(metric_name="macro_key", expression="COUNT(*)"),
            ],
            database=Database(database_name="my_database", sqlalchemy_uri="sqlite://"),
            schema="my_schema",
            sql=None,
        ),
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    env = SandboxedEnvironment(undefined=DebugUndefined)
    # Getting the data from the request context (the request body envelope).
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "slice_id": 1,
                    }
                }
            ],
        }
    )
    assert metric_macro(env, {}, "macro_key") == "COUNT(*)"

    # Getting data from g's form_data. In the Liteset port both the request
    # body envelope and ``g.form_data`` resolve to the single ``_form_data_ctx``
    # ContextVar, so the second upstream scenario uses the same source.
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "slice_id": 1,
                    }
                }
            ],
        }
    )
    assert metric_macro(env, {}, "macro_key") == "COUNT(*)"


def test_metric_macro_no_dataset_id_with_context_slice_id_none(
    mocker: MockerFixture,
) -> None:
    """
    Test the ``metric_macro`` when not specifying a dataset ID and context
    includes slice_id set to None (url_params.slice_id).
    """
    env = SandboxedEnvironment(undefined=DebugUndefined)
    # Getting the data from the request context (the request body envelope).
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "slice_id": None,
                    }
                }
            ],
        }
    )
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "macro_key")
    assert str(excinfo.value) == (
        "Please specify the Dataset ID for the ``macro_key`` metric in the Jinja macro."
    )

    # Getting data from g's form_data. In the Liteset port both the request
    # body envelope and ``g.form_data`` resolve to the single ``_form_data_ctx``
    # ContextVar, so the second upstream scenario uses the same source.
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "slice_id": None,
                    }
                }
            ],
        }
    )
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "macro_key")
    assert str(excinfo.value) == (
        "Please specify the Dataset ID for the ``macro_key`` metric in the Jinja macro."
    )


def test_metric_macro_no_dataset_id_with_context_deleted_chart(
    mocker: MockerFixture,
) -> None:
    """
    Test the ``metric_macro`` when not specifying a dataset ID and context
    includes a deleted chart ID.

    Upstream drives this via ``ChartDAO.find_by_id`` returning ``None``. The
    Liteset equivalent is the synchronous chart lookup inside
    ``_dataset_id_from_chart``; we patch the engine + ORM ``Session`` so the
    chart query returns ``None``, exercising the resolver's own
    ``if not chart: raise`` None-handling rather than mocking the raise itself.
    """
    mocker.patch("superset.jinja_context._get_sync_engine", return_value=MagicMock())

    # Build a Session context manager whose query yields no chart (None),
    # mirroring ``ChartDAO.find_by_id`` returning None for a deleted chart.
    session = MagicMock()
    session.execute.return_value.scalars.return_value.one_or_none.return_value = None
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    mocker.patch("sqlalchemy.orm.Session", return_value=session_cm)

    env = SandboxedEnvironment(undefined=DebugUndefined)
    # Getting the data from the request context (the request body envelope).
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "slice_id": 1,
                    }
                }
            ],
        }
    )
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "macro_key")
    assert str(excinfo.value) == (
        "Please specify the Dataset ID for the ``macro_key`` metric in the Jinja macro."
    )

    # Getting data from g's form_data. In the Liteset port both the request
    # body envelope and ``g.form_data`` resolve to the single ``_form_data_ctx``
    # ContextVar, so the second upstream scenario uses the same source.
    set_form_data(
        {
            "queries": [
                {
                    "url_params": {
                        "slice_id": 1,
                    }
                }
            ],
        }
    )
    with pytest.raises(SupersetTemplateException) as excinfo:
        metric_macro(env, {}, "macro_key")
    assert str(excinfo.value) == (
        "Please specify the Dataset ID for the ``macro_key`` metric in the Jinja macro."
    )


def test_metric_macro_no_dataset_id_available_in_request_form_data(
    mocker: MockerFixture,
) -> None:
    """
    Test the ``metric_macro`` when not specifying a dataset ID and context
    includes an existing dataset ID (datasource.id).
    """
    mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=SqlaTable(
            table_name="test_dataset",
            metrics=[
                SqlMetric(metric_name="macro_key", expression="COUNT(*)"),
            ],
            database=Database(database_name="my_database", sqlalchemy_uri="sqlite://"),
            schema="my_schema",
            sql=None,
        ),
    )
    mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    env = SandboxedEnvironment(undefined=DebugUndefined)
    set_form_data(
        {
            "datasource": {
                "id": 1,
            },
        }
    )
    assert metric_macro(env, {}, "macro_key") == "COUNT(*)"

    # ``datasource`` may also be a flat "id__type" string (warm_up_cache path).
    set_form_data(
        {
            "datasource": "1__table",
        }
    )
    assert metric_macro(env, {}, "macro_key") == "COUNT(*)"


def test_metric_macro_regular_user_uses_base_filter(mocker: MockerFixture) -> None:
    """
    Test that the ``metric_macro`` uses base filter for regular users.

    Regular users should have standard RBAC/RLS filters applied when accessing
    datasets, i.e. ``_sync_user_can_access_dataset`` is called with
    ``skip_base_filter=False``.
    """
    mocker.patch(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=lambda feature: False),
    )
    set_current_user(SimpleNamespace(id=1, is_guest=False, roles=[]))

    dataset = SqlaTable(
        table_name="test_dataset",
        metrics=[
            SqlMetric(metric_name="count", expression="COUNT(*)"),
        ],
        database=Database(database_name="my_database", sqlalchemy_uri="sqlite://"),
        schema="my_schema",
        sql=None,
    )
    find_dataset = mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    can_access = mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    env = SandboxedEnvironment(undefined=DebugUndefined)
    assert metric_macro(env, {}, "count", 1) == "COUNT(*)"

    # Upstream asserts ``DatasetDAO.find_by_id.assert_called_once_with(
    # 1, skip_base_filter=False)``. The Liteset port splits that single
    # filtered lookup into two seams: ``_sync_find_dataset(<id>)`` loads the
    # dataset and ``_sync_user_can_access_dataset(dataset, user,
    # skip_base_filter=...)`` enforces the base filter. Assert both: the
    # lookup ran exactly once with the positional dataset id ``1`` and the
    # access check ran exactly once without skipping the base filter.
    find_dataset.assert_called_once_with(1)
    can_access.assert_called_once_with(dataset, mocker.ANY, skip_base_filter=False)


def test_metric_macro_regular_user_raises_no_access(mocker: MockerFixture) -> None:
    """
    Test that the ``metric_macro`` raises for regular user without dataset access.
    """
    mocker.patch(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=lambda feature: False),
    )
    set_current_user(SimpleNamespace(id=1, is_guest=False, roles=[]))

    dataset = SqlaTable(
        table_name="test_dataset",
        metrics=[
            SqlMetric(metric_name="count", expression="COUNT(*)"),
        ],
        database=Database(database_name="my_database", sqlalchemy_uri="sqlite://"),
        schema="my_schema",
        sql=None,
    )
    find_dataset = mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    can_access = mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=False,
    )

    env = SandboxedEnvironment(undefined=DebugUndefined)
    with pytest.raises(DatasetNotFoundError) as excinfo:
        metric_macro(env, {}, "count", 1)

    assert str(excinfo.value) == "Dataset ID 1 not found."
    # Upstream asserts ``DatasetDAO.find_by_id.assert_called_once_with(
    # 1, skip_base_filter=False)``. On the split Liteset seams, assert the
    # lookup ran exactly once with positional id ``1`` and the access check
    # ran exactly once without skipping the base filter.
    find_dataset.assert_called_once_with(1)
    can_access.assert_called_once_with(dataset, mocker.ANY, skip_base_filter=False)


def test_metric_macro_embedded_user_skips_base_filter(mocker: MockerFixture) -> None:
    """
    Test that the ``metric_macro`` skips base filter for embedded users.

    Embedded (guest) users have dashboard-level access control via their
    embedding token, so the regular dataset base filter is bypassed, i.e.
    ``_sync_user_can_access_dataset`` is called with ``skip_base_filter=True``.
    """
    mocker.patch(
        "superset.jinja_context.feature_flag_manager",
        MagicMock(is_feature_enabled=lambda feature: feature == "EMBEDDED_SUPERSET"),
    )
    set_current_user(SimpleNamespace(username="guest", is_guest=True, roles=[]))

    dataset = SqlaTable(
        table_name="test_dataset",
        metrics=[
            SqlMetric(metric_name="count", expression="COUNT(*)"),
        ],
        database=Database(database_name="my_database", sqlalchemy_uri="sqlite://"),
        schema="my_schema",
        sql=None,
    )
    find_dataset = mocker.patch(
        "superset.jinja_context._sync_find_dataset",
        return_value=dataset,
    )
    can_access = mocker.patch(
        "superset.jinja_context._sync_user_can_access_dataset",
        return_value=True,
    )

    env = SandboxedEnvironment(undefined=DebugUndefined)
    assert metric_macro(env, {}, "count", 1) == "COUNT(*)"

    # Upstream asserts ``DatasetDAO.find_by_id.assert_called_once_with(
    # 1, skip_base_filter=True)``. On the split Liteset seams, assert the
    # lookup ran exactly once with positional id ``1`` and the access check
    # ran exactly once skipping the base filter for the guest user.
    find_dataset.assert_called_once_with(1)
    can_access.assert_called_once_with(dataset, mocker.ANY, skip_base_filter=True)


@pytest.mark.parametrize(
    "description,args,kwargs,sqlalchemy_uri,query,time_filter,removed_filters,applied_filters",
    [
        (
            "Missing time_range and filter will return a No filter result",
            [],
            {"target_type": "TIMESTAMP"},
            "postgresql://mydb",
            {},
            TimeFilter(
                from_expr=None,
                to_expr=None,
                time_range="No filter",
            ),
            [],
            [],
        ),
        (
            "Missing time range and filter with default value will return a result with the defaults",  # noqa: E501
            [],
            {"default": "Last week", "target_type": "TIMESTAMP"},
            "postgresql://mydb",
            {},
            TimeFilter(
                from_expr="TO_TIMESTAMP('2024-08-27 00:00:00.000000', 'YYYY-MM-DD HH24:MI:SS.US')",  # noqa: E501
                to_expr="TO_TIMESTAMP('2024-09-03 00:00:00.000000', 'YYYY-MM-DD HH24:MI:SS.US')",  # noqa: E501
                time_range="Last week",
            ),
            [],
            [],
        ),
        (
            "Time range is extracted with the expected format, and default is ignored",
            [],
            {"default": "Last month", "target_type": "TIMESTAMP"},
            "postgresql://mydb",
            {"time_range": "Last week"},
            TimeFilter(
                from_expr="TO_TIMESTAMP('2024-08-27 00:00:00.000000', 'YYYY-MM-DD HH24:MI:SS.US')",  # noqa: E501
                to_expr="TO_TIMESTAMP('2024-09-03 00:00:00.000000', 'YYYY-MM-DD HH24:MI:SS.US')",  # noqa: E501
                time_range="Last week",
            ),
            [],
            [],
        ),
        (
            "Filter is extracted with the native format of the column (TIMESTAMP)",
            ["dttm"],
            {},
            "postgresql://mydb",
            {
                "filters": [
                    {
                        "col": "dttm",
                        "op": "TEMPORAL_RANGE",
                        "val": "Last week",
                    },
                ],
            },
            TimeFilter(
                from_expr="TO_TIMESTAMP('2024-08-27 00:00:00.000000', 'YYYY-MM-DD HH24:MI:SS.US')",  # noqa: E501
                to_expr="TO_TIMESTAMP('2024-09-03 00:00:00.000000', 'YYYY-MM-DD HH24:MI:SS.US')",  # noqa: E501
                time_range="Last week",
            ),
            [],
            ["dttm"],
        ),
        (
            "Filter is extracted with the native format of the column (DATE)",
            ["dt"],
            {"remove_filter": True},
            "postgresql://mydb",
            {
                "filters": [
                    {
                        "col": "dt",
                        "op": "TEMPORAL_RANGE",
                        "val": "Last week",
                    },
                ],
            },
            TimeFilter(
                from_expr="TO_DATE('2024-08-27', 'YYYY-MM-DD')",
                to_expr="TO_DATE('2024-09-03', 'YYYY-MM-DD')",
                time_range="Last week",
            ),
            ["dt"],
            ["dt"],
        ),
        (
            "Filter is extracted with the overridden format (TIMESTAMP to DATE)",
            ["dttm"],
            {"target_type": "DATE", "remove_filter": True},
            "trino://mydb",
            {
                "filters": [
                    {
                        "col": "dttm",
                        "op": "TEMPORAL_RANGE",
                        "val": "Last month",
                    },
                ],
            },
            TimeFilter(
                from_expr="DATE '2024-08-03'",
                to_expr="DATE '2024-09-03'",
                time_range="Last month",
            ),
            ["dttm"],
            ["dttm"],
        ),
        (
            "Filter is formatted with the custom format, ignoring target_type",
            ["dttm"],
            {"target_type": "DATE", "strftime": "%Y%m%d", "remove_filter": True},
            "trino://mydb",
            {
                "filters": [
                    {
                        "col": "dttm",
                        "op": "TEMPORAL_RANGE",
                        "val": "Last month",
                    },
                ],
            },
            TimeFilter(
                from_expr="20240803",
                to_expr="20240903",
                time_range="Last month",
            ),
            ["dttm"],
            ["dttm"],
        ),
    ],
)
def test_get_time_filter(
    description: str,
    args: list[Any],
    kwargs: dict[str, Any],
    sqlalchemy_uri: str,
    query: dict[str, Any],
    time_filter: TimeFilter,
    removed_filters: list[str],
    applied_filters: list[str],
) -> None:
    """
    Test the ``get_time_filter`` macro.

    Upstream wraps the query content under a ``queries`` list in the request
    body; the controller extracts the active query into ``form_data`` before
    Jinja rendering. The Liteset unit test passes that single query's content
    directly as ``form_data``.
    """
    columns = [
        TableColumn(column_name="dt", is_dttm=1, type="DATE"),
        TableColumn(column_name="dttm", is_dttm=1, type="TIMESTAMP"),
    ]

    database = Database(database_name="my_database", sqlalchemy_uri=sqlalchemy_uri)
    table = SqlaTable(
        table_name="my_dataset",
        columns=columns,
        main_dttm_col="dt",
        database=database,
    )

    with freeze_time("2024-09-03"):
        cache = ExtraCache(
            database=database,
            table=table,
            form_data=query,
        )

        assert cache.get_time_filter(*args, **kwargs) == time_filter, description
        assert cache.removed_filters == removed_filters
        assert cache.applied_filters == applied_filters


def test_jinja2_template_syntax_error_handling(mocker: MockerFixture) -> None:
    """Test TemplateSyntaxError handling with proper error message and 422 status"""
    from superset.errors import SupersetErrorType
    from superset.exceptions import SupersetSyntaxErrorException

    database = mocker.MagicMock()
    database.db_engine_spec = mocker.MagicMock()

    from superset.jinja_context import BaseTemplateProcessor

    processor = BaseTemplateProcessor(database=database)

    # Test with invalid Jinja2 syntax
    template = "SELECT * WHERE column = {{ variable such as 'default' }}"

    with pytest.raises(SupersetSyntaxErrorException) as exc_info:
        processor.process_template(template)

    exception = exc_info.value
    assert len(exception.errors) == 1
    error = exception.errors[0]

    # Verify error message contains helpful guidance
    assert "Jinja2 template error" in error.message
    assert "TemplateSyntaxError" in error.message
    assert "expected token" in error.message

    # Verify error type and status
    assert error.error_type == SupersetErrorType.GENERIC_COMMAND_ERROR
    assert exception.status == 422

    # Verify extra data includes template snippet
    assert "template" in error.extra
    assert error.extra["template"][:50] == template[:50]


def test_jinja2_undefined_error_handling(mocker: MockerFixture) -> None:
    """Test that UndefinedError is handled as client error"""
    from jinja2.exceptions import UndefinedError

    from superset.exceptions import SupersetSyntaxErrorException

    database = mocker.MagicMock()
    database.db_engine_spec = mocker.MagicMock()

    from superset.jinja_context import BaseTemplateProcessor

    processor = BaseTemplateProcessor(database=database)
    template = "SELECT * FROM table"

    # Mock the Environment.from_string to raise UndefinedError
    with patch.object(
        processor.env, "from_string", side_effect=UndefinedError("Variable not defined")
    ):
        with pytest.raises(SupersetSyntaxErrorException) as exc_info:
            processor.process_template(template)

        exception = exc_info.value
        error = exception.errors[0]

        # Should get client error message (422)
        assert "Jinja2 template error" in error.message
        assert "UndefinedError" in error.message
        assert "Variable not defined" in error.message
        assert exception.status == 422


def test_jinja2_security_error_handling(mocker: MockerFixture) -> None:
    """Test that SecurityError is handled as client error"""
    from jinja2.exceptions import SecurityError

    from superset.exceptions import SupersetSyntaxErrorException

    database = mocker.MagicMock()
    database.db_engine_spec = mocker.MagicMock()

    from superset.jinja_context import BaseTemplateProcessor

    processor = BaseTemplateProcessor(database=database)
    template = "SELECT * FROM table"

    # Mock the Environment.from_string to raise SecurityError
    with patch.object(
        processor.env, "from_string", side_effect=SecurityError("Access denied")
    ):
        with pytest.raises(SupersetSyntaxErrorException) as exc_info:
            processor.process_template(template)

        exception = exc_info.value
        error = exception.errors[0]

        # Should get client error message with SecurityError type
        assert "Jinja2 template error" in error.message
        assert "SecurityError" in error.message
        assert "Access denied" in error.message
        assert exception.status == 422


def test_jinja2_server_error_handling(mocker: MockerFixture) -> None:
    """Test that server errors (like MemoryError) are handled with 500 status"""
    database = mocker.MagicMock()
    database.db_engine_spec = mocker.MagicMock()

    from superset.jinja_context import BaseTemplateProcessor

    processor = BaseTemplateProcessor(database=database)
    template = "SELECT * FROM table"

    # Mock the Environment.from_string to raise MemoryError (server error)
    with patch.object(
        processor.env, "from_string", side_effect=MemoryError("Out of memory")
    ):
        with pytest.raises(SupersetTemplateException) as exc_info:
            processor.process_template(template)

        exception = exc_info.value

        # Should get server error message (500)
        assert "Internal Jinja2 template error" in str(exception)
        assert "MemoryError" in str(exception)
        assert "Out of memory" in str(exception)
