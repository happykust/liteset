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
"""Unit tests for superset.utils.core helpers."""

from __future__ import annotations

import pytest

from superset.utils.core import split_adhoc_filters_into_base_filters


def test_split_adhoc_filters_sanitizes_sql_clause_comment():
    """SQL adhoc filter expressions are run through sanitize_clause, which
    strips trailing SQL comments. Without it a ``--`` comment would comment
    out the rest of the assembled WHERE clause (joined with `` AND ``)."""
    form_data = {
        "adhoc_filters": [
            {
                "expressionType": "SQL",
                "clause": "WHERE",
                "sqlExpression": "a = 1 -- malicious",
            }
        ]
    }
    split_adhoc_filters_into_base_filters(form_data, "postgresql")
    assert form_data["where"] == "(a = 1)"


def test_split_adhoc_filters_sanitizes_having_clause():
    form_data = {
        "adhoc_filters": [
            {
                "expressionType": "SQL",
                "clause": "HAVING",
                "sqlExpression": "count(*) > 1 -- x",
            }
        ]
    }
    split_adhoc_filters_into_base_filters(form_data, "postgresql")
    # sanitize_clause strips the comment and normalises via sqlglot
    # (function names upper-cased) — the comment must be gone.
    assert form_data["having"] == "(COUNT(*) > 1)"
    assert "--" not in form_data["having"]


def test_split_adhoc_filters_simple_where_unchanged():
    """SIMPLE filters are restructured into the structured ``filters`` list."""
    form_data = {
        "adhoc_filters": [
            {
                "expressionType": "SIMPLE",
                "clause": "WHERE",
                "subject": "col",
                "operator": "==",
                "comparator": "x",
            }
        ]
    }
    split_adhoc_filters_into_base_filters(form_data, "postgresql")
    assert form_data["filters"] == [{"col": "col", "op": "==", "val": "x"}]
    assert form_data["where"] == ""


def test_split_adhoc_filters_invalid_sql_raises():
    """Malformed SQL in an adhoc filter is rejected by sanitize_clause."""
    from superset.exceptions import QueryClauseValidationException

    form_data = {
        "adhoc_filters": [
            {
                "expressionType": "SQL",
                "clause": "WHERE",
                "sqlExpression": "a = 1; DROP TABLE users",
            }
        ]
    }
    with pytest.raises(QueryClauseValidationException):
        split_adhoc_filters_into_base_filters(form_data, "postgresql")
