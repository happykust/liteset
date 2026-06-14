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

from datetime import datetime, timezone

import numpy as np
import pandas as pd
from numpy.core.multiarray import array
from pytest_mock import MockerFixture

from superset.dataframe import df_to_records
from superset.db_engine_specs.base import BaseEngineSpec
from superset.result_set import dedup, stringify_values, SupersetResultSet
from superset.typing import DbapiResult, GenericDataType


def test_column_names_as_bytes() -> None:
    """
    Test that we can handle column names as bytes.
    """
    from superset.db_engine_specs.redshift import RedshiftEngineSpec

    data = (
        [
            "2016-01-26",
            392.002014,
            397.765991,
            390.575012,
            392.153015,
            392.153015,
            58147000,
        ],
        [
            "2016-01-27",
            392.444,
            396.842987,
            391.782013,
            394.971985,
            394.971985,
            47424400,
        ],
    )
    description = [
        (b"date", 1043, None, None, None, None, None),
        (b"open", 701, None, None, None, None, None),
        (b"high", 701, None, None, None, None, None),
        (b"low", 701, None, None, None, None, None),
        (b"close", 701, None, None, None, None, None),
        (b"adj close", 701, None, None, None, None, None),
        (b"volume", 20, None, None, None, None, None),
    ]
    result_set = SupersetResultSet(data, description, RedshiftEngineSpec)  # type: ignore

    assert (
        result_set.to_pandas_df().to_markdown()
        == """
|    | date       |    open |    high |     low |   close |   adj close |   volume |
|---:|:-----------|--------:|--------:|--------:|--------:|------------:|---------:|
|  0 | 2016-01-26 | 392.002 | 397.766 | 390.575 | 392.153 |     392.153 | 58147000 |
|  1 | 2016-01-27 | 392.444 | 396.843 | 391.782 | 394.972 |     394.972 | 47424400 |
    """.strip()
    )


def test_stringify_with_null_integers() -> None:
    """
    Test that we can safely handle type errors when an integer column has a null value
    """
    data = [
        ("foo", "bar", pd.NA, None),
        ("foo", "bar", pd.NA, True),
        ("foo", "bar", pd.NA, None),
    ]
    numpy_dtype = [
        ("id", "object"),
        ("value", "object"),
        ("num", "object"),
        ("bool", "object"),
    ]

    array2 = np.array(data, dtype=numpy_dtype)
    column_names = ["id", "value", "num", "bool"]

    result_set = np.array([stringify_values(array2[column]) for column in column_names])

    expected = np.array(
        [
            array(["foo", "foo", "foo"], dtype=object),
            array(["bar", "bar", "bar"], dtype=object),
            array([None, None, None], dtype=object),
            array([None, "True", None], dtype=object),
        ]
    )

    assert np.array_equal(result_set, expected)


def test_stringify_with_null_timestamps() -> None:
    """
    Test that we can safely handle type errors when a timestamp column has a null value
    """
    data = [
        ("foo", "bar", pd.NaT, None),
        ("foo", "bar", pd.NaT, True),
        ("foo", "bar", pd.NaT, None),
    ]
    numpy_dtype = [
        ("id", "object"),
        ("value", "object"),
        ("num", "object"),
        ("bool", "object"),
    ]

    array2 = np.array(data, dtype=numpy_dtype)
    column_names = ["id", "value", "num", "bool"]

    result_set = np.array([stringify_values(array2[column]) for column in column_names])

    expected = np.array(
        [
            array(["foo", "foo", "foo"], dtype=object),
            array(["bar", "bar", "bar"], dtype=object),
            array([None, None, None], dtype=object),
            array([None, "True", None], dtype=object),
        ]
    )

    assert np.array_equal(result_set, expected)


def test_timezone_series(mocker: MockerFixture) -> None:
    """
    Test that we can handle timezone-aware datetimes correctly.

    This covers a regression that happened when upgrading from Pandas 1.5.3 to 2.0.3.
    """
    logger = mocker.patch("superset.result_set.logger")

    data = [[datetime(2023, 1, 1, tzinfo=timezone.utc)]]
    description = [(b"__time", "datetime", None, None, None, None, False)]
    result_set = SupersetResultSet(
        data,
        description,  # type: ignore
        BaseEngineSpec,
    )
    assert result_set.to_pandas_df().values.tolist() == [
        [pd.Timestamp("2023-01-01 00:00:00+0000", tz="UTC")]
    ]
    logger.exception.assert_not_called()


def test_get_column_description_from_empty_data_using_cursor_description(
    mocker: MockerFixture,
) -> None:
    """
    Test that we can handle get_column_decription from the cursor description
    when data is empty
    """
    logger = mocker.patch("superset.result_set.logger")

    data: DbapiResult = []
    description = [(b"__time", "datetime", None, None, None, None, 1, 0, 255)]
    result_set = SupersetResultSet(
        data,
        description,  # type: ignore
        BaseEngineSpec,
    )
    assert any(col.get("column_name") == "__time" for col in result_set.columns)
    logger.exception.assert_not_called()


def test_dedup() -> None:
    assert dedup(["foo", "bar"]) == ["foo", "bar"]
    assert dedup(["foo", "bar", "foo", "bar", "Foo"]) == [
        "foo",
        "bar",
        "foo__1",
        "bar__1",
        "Foo",
    ]
    assert dedup(["foo", "bar", "bar", "bar", "Bar"]) == [
        "foo",
        "bar",
        "bar__1",
        "bar__2",
        "Bar",
    ]
    assert dedup(["foo", "bar", "bar", "bar", "Bar"], case_sensitive=False) == [
        "foo",
        "bar",
        "bar__1",
        "bar__2",
        "Bar__3",
    ]


def test_get_columns_basic() -> None:
    data = [("a1", "b1", "c1"), ("a2", "b2", "c2")]
    cursor_descr = (("a", "string"), ("b", "string"), ("c", "string"))
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns == [
        {
            "is_dttm": False,
            "type": "STRING",
            "type_generic": GenericDataType.STRING,
            "column_name": "a",
            "name": "a",
        },
        {
            "is_dttm": False,
            "type": "STRING",
            "type_generic": GenericDataType.STRING,
            "column_name": "b",
            "name": "b",
        },
        {
            "is_dttm": False,
            "type": "STRING",
            "type_generic": GenericDataType.STRING,
            "column_name": "c",
            "name": "c",
        },
    ]


def test_get_columns_with_int() -> None:
    data = [("a1", 1), ("a2", 2)]
    cursor_descr = (("a", "string"), ("b", "int"))
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns == [
        {
            "is_dttm": False,
            "type": "STRING",
            "type_generic": GenericDataType.STRING,
            "column_name": "a",
            "name": "a",
        },
        {
            "is_dttm": False,
            "type": "INT",
            "type_generic": GenericDataType.NUMERIC,
            "column_name": "b",
            "name": "b",
        },
    ]


def test_get_columns_type_inference() -> None:
    data = [
        (1.2, 1, "foo", datetime(2018, 10, 19, 23, 39, 16, 660000), True),
        (3.14, 2, "bar", datetime(2019, 10, 19, 23, 39, 16, 660000), False),
    ]
    cursor_descr = (("a", None), ("b", None), ("c", None), ("d", None), ("e", None))
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns == [
        {
            "is_dttm": False,
            "type": "FLOAT",
            "type_generic": GenericDataType.NUMERIC,
            "column_name": "a",
            "name": "a",
        },
        {
            "is_dttm": False,
            "type": "INT",
            "type_generic": GenericDataType.NUMERIC,
            "column_name": "b",
            "name": "b",
        },
        {
            "is_dttm": False,
            "type": "STRING",
            "type_generic": GenericDataType.STRING,
            "column_name": "c",
            "name": "c",
        },
        {
            "is_dttm": True,
            "type": "DATETIME",
            "type_generic": GenericDataType.TEMPORAL,
            "column_name": "d",
            "name": "d",
        },
        {
            "is_dttm": False,
            "type": "BOOL",
            "type_generic": GenericDataType.BOOLEAN,
            "column_name": "e",
            "name": "e",
        },
    ]


def test_is_date() -> None:
    data = [("a", 1), ("a", 2)]
    cursor_descr = (("a", "string"), ("a", "string"))
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.is_temporal("DATE") is True
    assert results.is_temporal("DATETIME") is True
    assert results.is_temporal("TIME") is True
    assert results.is_temporal("TIMESTAMP") is True
    assert results.is_temporal("STRING") is False
    assert results.is_temporal("") is False
    assert results.is_temporal(None) is False


def test_dedup_with_data() -> None:
    data = [("a", 1), ("a", 2)]
    cursor_descr = (("a", "string"), ("a", "string"))
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    column_names = [col["column_name"] for col in results.columns]
    assert column_names == ["a", "a__1"]


def test_int64_with_missing_data() -> None:
    data = [(None,), (1239162456494753670,), (None,), (None,), (None,), (None,)]
    cursor_descr = [("user_id", "bigint", None, None, None, None, True)]
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns[0]["type"] == "BIGINT"
    assert results.columns[0]["type_generic"] == GenericDataType.NUMERIC


def test_data_as_list_of_lists() -> None:
    data = [[1, "a"], [2, "b"]]
    cursor_descr = [
        ("user_id", "INT", None, None, None, None, True),
        ("username", "STRING", None, None, None, None, True),
    ]
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    df = results.to_pandas_df()
    assert df_to_records(df) == [
        {"user_id": 1, "username": "a"},
        {"user_id": 2, "username": "b"},
    ]


def test_nullable_bool() -> None:
    data = [(None,), (True,), (None,), (None,), (None,), (None,)]
    cursor_descr = [("is_test", "bool", None, None, None, None, True)]
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns[0]["type"] == "BOOL"
    assert results.columns[0]["type_generic"] == GenericDataType.BOOLEAN
    df = results.to_pandas_df()
    assert df_to_records(df) == [
        {"is_test": None},
        {"is_test": True},
        {"is_test": None},
        {"is_test": None},
        {"is_test": None},
        {"is_test": None},
    ]


def test_nested_types() -> None:
    data = [
        (
            4,
            [{"table_name": "unicode_test", "database_id": 1}],
            [1, 2, 3],
            {"chart_name": "scatter"},
        ),
        (
            3,
            [{"table_name": "birth_names", "database_id": 1}],
            [4, 5, 6],
            {"chart_name": "plot"},
        ),
    ]
    cursor_descr = [("id",), ("dict_arr",), ("num_arr",), ("map_col",)]
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns[0]["type"] == "INT"
    assert results.columns[0]["type_generic"] == GenericDataType.NUMERIC
    assert results.columns[1]["type"] == "STRING"
    assert results.columns[1]["type_generic"] == GenericDataType.STRING
    assert results.columns[2]["type"] == "STRING"
    assert results.columns[2]["type_generic"] == GenericDataType.STRING
    assert results.columns[3]["type"] == "STRING"
    assert results.columns[3]["type_generic"] == GenericDataType.STRING
    df = results.to_pandas_df()
    assert df_to_records(df) == [
        {
            "id": 4,
            "dict_arr": '[{"table_name": "unicode_test", "database_id": 1}]',
            "num_arr": "[1, 2, 3]",
            "map_col": "{'chart_name': 'scatter'}",
        },
        {
            "id": 3,
            "dict_arr": '[{"table_name": "birth_names", "database_id": 1}]',
            "num_arr": "[4, 5, 6]",
            "map_col": "{'chart_name': 'plot'}",
        },
    ]


def test_single_column_multidim_nested_types() -> None:
    data = [
        (
            [
                "test",
                [
                    [
                        "foo",
                        123456,
                        [
                            [["test"], 3432546, 7657658766],
                            [["fake"], 656756765, 324324324324],
                        ],
                    ]
                ],
                ["test2", 43, 765765765],
                None,
                None,
            ],
        )
    ]
    cursor_descr = [("metadata",)]
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns[0]["type"] == "STRING"
    assert results.columns[0]["type_generic"] == GenericDataType.STRING
    df = results.to_pandas_df()
    assert df_to_records(df) == [
        {
            "metadata": '["test", [["foo", 123456, [[["test"], 3432546, 7657658766], [["fake"], 656756765, 324324324324]]]], ["test2", 43, 765765765], null, null]'  # noqa: E501
        }
    ]


def test_nested_list_types() -> None:
    data = [([{"TestKey": [123456, "foo"]}],)]
    cursor_descr = [("metadata",)]
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns[0]["type"] == "STRING"
    assert results.columns[0]["type_generic"] == GenericDataType.STRING
    df = results.to_pandas_df()
    assert df_to_records(df) == [{"metadata": '[{"TestKey": [123456, "foo"]}]'}]


def test_empty_datetime() -> None:
    data = [(None,)]
    cursor_descr = [("ds", "timestamp", None, None, None, None, True)]
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns[0]["type"] == "TIMESTAMP"
    assert results.columns[0]["type_generic"] == GenericDataType.TEMPORAL


def test_no_type_coercion() -> None:
    data = [("a", 1), ("b", 2)]
    cursor_descr = [
        ("one", "varchar", None, None, None, None, True),
        ("two", "int", None, None, None, None, True),
    ]
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert results.columns[0]["type"] == "VARCHAR"
    assert results.columns[0]["type_generic"] == GenericDataType.STRING
    assert results.columns[1]["type"] == "INT"
    assert results.columns[1]["type_generic"] == GenericDataType.NUMERIC


def test_empty_data() -> None:
    data: DbapiResult = []
    cursor_descr = [
        ("emptyone", "varchar", None, None, None, None, True),
        ("emptytwo", "int", None, None, None, None, True),
    ]
    results = SupersetResultSet(data, cursor_descr, BaseEngineSpec)
    assert len(results.columns) == 2
