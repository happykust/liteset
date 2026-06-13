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
"""Unit tests for Database.get_df mutator and post_process_df behavior.

1:1 contract tests for superset_old/models/core.py lines 715-718:
  - if mutator: df = mutator(df)   # unconditional reassignment
  - return self.post_process_df(df)  # no None guard before this call

These tests assert the ORIGINAL Superset behaviour is preserved:
  - mutator returning None overwrites df with None.
  - post_process_df(None) raises AttributeError.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch, PropertyMock

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# post_process_df tests (staticmethod, no DB interaction needed)
# ---------------------------------------------------------------------------


def test_post_process_df_none_raises_attribute_error() -> None:
    """1:1 with original: post_process_df(None) raises AttributeError.

    In superset_old/models/core.py line 639:
        for col, coltype in df.dtypes.to_dict().items():
    When df is None, df.dtypes raises AttributeError.
    The original has NO None-guard before calling post_process_df (line 718),
    so all-DDL SQL (no SELECT rows) crashes with AttributeError.
    """
    from superset.models.core import Database

    with pytest.raises(AttributeError):
        Database.post_process_df(None)  # type: ignore[arg-type]


def test_post_process_df_empty_dataframe() -> None:
    """Normal case: empty DataFrame returns successfully."""
    from superset.models.core import Database

    result = Database.post_process_df(pd.DataFrame())
    assert isinstance(result, pd.DataFrame)
    assert result.empty


def test_post_process_df_simple_dataframe() -> None:
    """Normal case: plain-value DataFrame is returned unchanged."""
    from superset.models.core import Database

    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    result = Database.post_process_df(df)
    assert list(result.columns) == ["a", "b"]
    assert list(result["a"]) == [1, 2]


# ---------------------------------------------------------------------------
# get_df mutator contract tests
# ---------------------------------------------------------------------------


def _make_wired_db(
    source_df: pd.DataFrame | None,
) -> tuple[object, MagicMock, MagicMock, MagicMock]:
    """Return (db, fake_engine, fake_conn, engine_spec) with I/O wired."""
    from superset.models.core import Database

    engine_spec = MagicMock()
    engine_spec.engine = "postgresql"
    engine_spec.get_prequeries.return_value = []
    fake_engine = MagicMock()
    fake_engine.url = "postgresql:///"
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    @contextmanager  # type: ignore[misc]
    def fake_engine_ctx(**_kw):
        yield fake_engine

    db = Database.__new__(Database)
    db.get_sqla_engine = fake_engine_ctx  # type: ignore[method-assign]
    db.mutate_sql_based_on_config = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda s, **_k: s
    )
    db._resolve_log_query = MagicMock(return_value=None)  # type: ignore[method-assign]
    db._call_log_query = MagicMock()  # type: ignore[method-assign]
    if source_df is not None:
        db.fetch_rows = MagicMock(return_value=[(1,), (2,)])  # type: ignore[method-assign]
        db.load_into_dataframe = MagicMock(  # type: ignore[method-assign]
            return_value=source_df
        )
    else:
        # Simulate all-DDL: fetch_rows always returns None
        db.fetch_rows = MagicMock(return_value=None)  # type: ignore[method-assign]
        db.load_into_dataframe = MagicMock()  # type: ignore[method-assign]
    return db, fake_engine, fake_conn, engine_spec


def test_get_df_mutator_returning_none_propagates_to_post_process() -> None:
    """1:1 with original: mutator returning None overwrites df.

    Original superset_old/models/core.py lines 715-718:
        if mutator:
            df = mutator(df)   # unconditional — None return replaces df
        return self.post_process_df(df)  # df is now None -> AttributeError

    post_process_df is patched to record what value it receives.
    """
    from superset.models.core import Database

    source_df = pd.DataFrame({"x": [1, 2]})
    received: list[object] = []

    def none_mutator(df: pd.DataFrame) -> None:  # type: ignore[return-value]
        return None

    def recording_post_process(df: object) -> object:
        received.append(df)
        if df is None:
            raise AttributeError("crash on None df")
        return df

    db, fake_engine, fake_conn, engine_spec = _make_wired_db(source_df)

    spec_patch = patch.object(
        Database, "db_engine_spec", new_callable=PropertyMock, return_value=engine_spec
    )
    ppdf_patch = patch.object(
        Database, "post_process_df", staticmethod(recording_post_process)
    )
    with spec_patch, ppdf_patch, patch("contextlib.closing") as mc:
        mc.return_value.__enter__ = MagicMock(return_value=fake_conn)
        mc.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(AttributeError, match="crash on None df"):
            db.get_df("SELECT x FROM t", mutator=none_mutator)  # type: ignore[attr-defined]

    # post_process_df must have been called with None, not source_df
    assert len(received) == 1
    assert received[0] is None


def test_get_df_mutator_returning_dataframe_used() -> None:
    """Normal case: mutator returning a new DataFrame replaces df correctly."""
    from superset.models.core import Database

    original_df = pd.DataFrame({"x": [1, 2]})
    mutated_df = pd.DataFrame({"x": [10, 20]})
    received: list[object] = []

    def transform_mutator(df: pd.DataFrame) -> pd.DataFrame:
        return mutated_df

    def recording_post_process(df: object) -> object:
        received.append(df)
        return df

    db, fake_engine, fake_conn, engine_spec = _make_wired_db(original_df)

    spec_patch = patch.object(
        Database, "db_engine_spec", new_callable=PropertyMock, return_value=engine_spec
    )
    ppdf_patch = patch.object(
        Database, "post_process_df", staticmethod(recording_post_process)
    )
    with spec_patch, ppdf_patch, patch("contextlib.closing") as mc:
        mc.return_value.__enter__ = MagicMock(return_value=fake_conn)
        mc.return_value.__exit__ = MagicMock(return_value=False)

        db.get_df("SELECT x FROM t", mutator=transform_mutator)  # type: ignore[attr-defined]

    # post_process_df must have been called with mutated_df, not original_df
    assert len(received) == 1
    assert received[0] is mutated_df


# ---------------------------------------------------------------------------
# get_raw_connection
# ---------------------------------------------------------------------------


def test_get_raw_connection_yields_conn_and_runs_prequeries() -> None:
    """get_raw_connection opens a sync engine, runs the engine-spec prequeries,
    and yields the raw DBAPI connection — 1:1 with upstream
    Database.get_raw_connection. The sync engine-spec helpers
    (estimate_query_cost, BigQuery get_latest_partition, GSheets
    get_table_metadata) call it and would AttributeError without it."""
    from superset.models.core import Database

    db = Database.__new__(Database)

    fake_cursor = MagicMock()
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor
    fake_engine = MagicMock()
    fake_engine.raw_connection.return_value = fake_conn

    @contextmanager
    def fake_engine_ctx(*_a, **_k):
        yield fake_engine

    spec = MagicMock()
    spec.get_prequeries.return_value = ["SET search_path = myschema"]

    with (
        patch.object(Database, "get_sqla_engine", fake_engine_ctx),
        patch.object(
            Database, "db_engine_spec", new_callable=PropertyMock, return_value=spec
        ),
    ):
        with db.get_raw_connection(catalog="c", schema="myschema") as conn:
            assert conn is fake_conn

    spec.get_prequeries.assert_called_once()
    fake_cursor.execute.assert_called_once_with("SET search_path = myschema")
    # closing() must have closed the raw connection on exit.
    fake_conn.close.assert_called_once()
