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
"""Regression for ``_apply_ctas`` auto-generating ``tmp_table_name``.

A CTAS with an empty ``tmp_table_name`` makes the server auto-generate one
from ``query.start_time``. That column is ``Numeric`` so SQLAlchemy returns a
``Decimal``, and ``datetime.fromtimestamp(Decimal / 1000)`` raised
``TypeError: 'decimal.Decimal' object cannot be interpreted as an integer``
-> HTTP 500. ``_apply_ctas`` must cast to ``float``.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from superset.sql.parse import SQLScript
from superset.tasks.sql_lab import _apply_ctas


def _query(tmp_name: str, start_time: object) -> SimpleNamespace:
    return SimpleNamespace(
        tmp_table_name=tmp_name,
        start_time=start_time,
        user_id=1,
        ctas_method="TABLE",
        tmp_schema_name="public",
        catalog=None,
        database=SimpleNamespace(
            db_engine_spec=SimpleNamespace(supports_cross_catalog_queries=False)
        ),
    )


def _last_stmt(sql: str):
    return SQLScript(sql, engine="postgresql").statements[-1]


def test_apply_ctas_generates_name_from_decimal_start_time() -> None:
    """Empty tmp_table_name + Decimal start_time -> generated name, no crash."""
    q = _query("", Decimal("1700000000000.0"))
    rewritten = _apply_ctas(q, _last_stmt("SELECT 1 AS x"))
    assert q.tmp_table_name.startswith("tmp_1_table_")
    assert "CREATE TABLE" in str(rewritten).upper()


def test_apply_ctas_keeps_explicit_name() -> None:
    """An explicit tmp_table_name is preserved (no generation)."""
    q = _query("my_table", Decimal("1700000000000.0"))
    rewritten = _apply_ctas(q, _last_stmt("SELECT 1 AS x"))
    assert q.tmp_table_name == "my_table"
    assert "my_table" in str(rewritten)
