#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.
"""Flask-free port of the vendored upstream
``tests/unit_tests/datasource/utils/test_replace_verbose_with_column.py``.

Liteset moved the helper from ``superset.views.datasource.utils`` to
``superset.controllers.legacy_datasource._replace_verbose_with_column`` (a 1:1
port).  The signature changed: the upstream helper took a plain list of column
objects as the second arg, whereas the Liteset port takes the *datasource*
object and reads ``datasource.columns``.  The fixtures below wrap the columns in
a tiny ``Datasource`` stand-in to bridge that difference; all the behavioural
cases (match / multi-match / no-match / empty / missing 'col' key / column
missing attrs) carry over unchanged.

Note: the Liteset port returns early when ``datasource.columns`` is empty, so
the "missing 'col' key" case is exercised with a non-empty column set (the
upstream original passed columns there anyway).
"""

import pytest

from superset.controllers.legacy_datasource import _replace_verbose_with_column


class Column:
    def __init__(self, column_name, verbose_name):
        self.column_name = column_name
        self.verbose_name = verbose_name


class IncompleteColumn:
    """A column missing required attributes."""

    def __init__(self, only_name):
        self.only_name = only_name


class Datasource:
    """Minimal stand-in exposing only the ``.columns`` attribute used."""

    def __init__(self, columns):
        self.columns = columns


# Test dataset and filters
columns = [
    Column("col1", "Column 1"),
    Column("col3", "Column 3"),
]


@pytest.mark.parametrize(
    "filters, expected",
    [
        # Normal match, should be replaced with the actual column_name
        ([{"col": "Column 1"}], [{"col": "col1"}]),
        # Multiple filters, should correctly replace all matching columns
        (
            [{"col": "Column 1"}, {"col": "Column 3"}],
            [{"col": "col1"}, {"col": "col3"}],
        ),
        # No matching case, the original value should remain unchanged
        ([{"col": "Non-existent"}], [{"col": "Non-existent"}]),
        # Empty filters, no changes should be made
        ([], []),
    ],
)
def test_replace_verbose_with_column(filters, expected):
    filters_copy = [dict(f) for f in filters]
    _replace_verbose_with_column(filters_copy, Datasource(columns))
    assert filters_copy == expected


def test_replace_verbose_with_column_missing_col_key(caplog):
    """Filter dict missing 'col' should trigger a warning and be skipped."""
    filters = [{"op": "=="}]  # missing "col"
    with caplog.at_level("WARNING"):
        _replace_verbose_with_column(filters, Datasource(columns))
    assert "Filter missing 'col' key:" in caplog.text
    # filter should remain unchanged
    assert filters == [{"op": "=="}]


def test_replace_verbose_with_column_missing_column_attrs(caplog):
    """Column missing expected attributes should trigger a warning."""
    filters = [{"col": "whatever"}]
    bad_columns = [IncompleteColumn("broken")]
    with caplog.at_level("WARNING"):
        _replace_verbose_with_column(filters, Datasource(bad_columns))
    assert "missing expected attributes" in caplog.text
    # filter should remain unchanged
    assert filters == [{"col": "whatever"}]
