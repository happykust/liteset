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
"""Flask-free port of ``tests/integration_tests/model_tests.py``.

Exercises the real Liteset ORM model methods against the seeded Postgres
backend:

* :class:`superset.models.core.Database` — engine construction, schema/catalog
  scoping, impersonation, DBAPI-exception mapping and ``select_star`` SQL
  generation.
* :class:`superset.models.connectors.SqlaTable` / ``TableColumn`` — timestamp
  expressions, query-string compilation, the ``SQL_QUERY_MUTATOR`` hook and the
  real query-execution pipeline.

The sync model methods (engine builders, ``get_query_str``, timestamp
expressions, lazy ``.database`` access) are driven through the port's sync
session (:func:`superset.db.session.get_sync_session`); the query-execution
cases drive the async pipeline (:meth:`SqlaTable.async_query`) since the port
does not implement the legacy synchronous ``SqlaTable.query`` execution path.
"""

from __future__ import annotations

import importlib.util
import re
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import select
from sqlalchemy.engine.url import make_url

from superset import config as superset_config
from superset.common.query_status import QueryStatus
from superset.db.session import get_sync_session
from superset.exceptions import SupersetException
from superset.models.connectors import SqlaTable
from superset.models.core import Database
from superset.models.slice import Slice  # noqa: F401
from superset.sql.parse import Table
from superset.utils import json  # noqa: F401
from superset.utils.core import DatasourceType, override_user  # noqa: F401
from superset.utils.database import get_example_database


def _is_module_installed(module_name: str) -> bool:
    """Mirror ``SupersetTestCase.is_module_installed`` without Flask imports."""
    return importlib.util.find_spec(module_name) is not None


def _get_table(name: str) -> SqlaTable:
    """Load a seeded dataset by name through the sync session.

    Replaces ``SupersetTestCase.get_table`` — the model methods exercised here
    (timestamp expressions, ``get_query_str``, lazy ``.database`` access) are
    synchronous, so they must run against the sync ORM session.
    """
    session = get_sync_session()
    table = (
        session.execute(select(SqlaTable).where(SqlaTable.table_name == name))
        .scalars()
        .first()
    )
    assert table is not None, f"seeded dataset {name!r} not found"
    return table


class _GammaUser:
    """Minimal stand-in for the ``gamma`` example user.

    The seeded DB has no users (see the integration factories), and the
    impersonation paths only read ``username`` / ``get_id`` off the current
    user, so a lightweight object reproduces the effect of
    ``security_manager.find_user(username="gamma")``.
    """

    def __init__(self) -> None:
        self.username = "gamma"
        self.id = 1

    def get_id(self) -> int:
        return self.id


class TestDatabaseModel:
    @pytest.mark.skipif(
        not _is_module_installed("requests"), reason="requests not installed"
    )
    @pytest.mark.skipif(
        not _is_module_installed("pyhive"), reason="pyhive not installed"
    )
    def test_database_schema_presto(self) -> None:
        sqlalchemy_uri = "presto://presto.airbnb.io:8080/hive/default"
        model = Database(database_name="test_database", sqlalchemy_uri=sqlalchemy_uri)

        with model.get_sqla_engine() as engine:
            db = make_url(engine.url).database
            assert db == "hive/default"

        with model.get_sqla_engine(schema="core_db") as engine:
            db = make_url(engine.url).database
            assert db == "hive/core_db"

        sqlalchemy_uri = "presto://presto.airbnb.io:8080/hive"
        model = Database(database_name="test_database", sqlalchemy_uri=sqlalchemy_uri)

        with model.get_sqla_engine() as engine:
            db = make_url(engine.url).database
            assert db == "hive"

        with model.get_sqla_engine(schema="core_db") as engine:
            db = make_url(engine.url).database
            assert db == "hive/core_db"

    def test_database_schema_postgres(self) -> None:
        sqlalchemy_uri = "postgresql+psycopg2://postgres.airbnb.io:5439/prod"
        model = Database(database_name="test_database", sqlalchemy_uri=sqlalchemy_uri)

        with model.get_sqla_engine() as engine:
            db = make_url(engine.url).database
            assert db == "prod"

        with model.get_sqla_engine(schema="foo") as engine:
            db = make_url(engine.url).database
            assert db == "prod"

    @pytest.mark.skipif(
        not _is_module_installed("thrift"), reason="thrift not installed"
    )
    @pytest.mark.skipif(
        not _is_module_installed("pyhive"), reason="pyhive not installed"
    )
    def test_database_schema_hive(self) -> None:
        sqlalchemy_uri = "hive://hive@hive.airbnb.io:10000/default?auth=NOSASL"
        model = Database(database_name="test_database", sqlalchemy_uri=sqlalchemy_uri)

        with model.get_sqla_engine() as engine:
            db = make_url(engine.url).database
            assert db == "default"

        with model.get_sqla_engine(schema="core_db") as engine:
            db = make_url(engine.url).database
            assert db == "core_db"

    @pytest.mark.skipif(
        not _is_module_installed("mysqlclient"), reason="mysqlclient not installed"
    )
    def test_database_schema_mysql(self) -> None:
        sqlalchemy_uri = "mysql://root@localhost/superset"
        model = Database(database_name="test_database", sqlalchemy_uri=sqlalchemy_uri)

        with model.get_sqla_engine() as engine:
            db = make_url(engine.url).database
            assert db == "superset"

        with model.get_sqla_engine(schema="staging") as engine:
            db = make_url(engine.url).database
            assert db == "staging"

    @pytest.mark.skipif(
        not _is_module_installed("mysqlclient"), reason="mysqlclient not installed"
    )
    def test_database_impersonate_user(self) -> None:
        uri = "mysql://root@localhost"
        example_user = _GammaUser()
        model = Database(database_name="test_database", sqlalchemy_uri=uri)

        with override_user(example_user):
            model.impersonate_user = True
            with model.get_sqla_engine() as engine:
                username = make_url(engine.url).username
                assert example_user.username == username

            model.impersonate_user = False
            with model.get_sqla_engine() as engine:
                username = make_url(engine.url).username
                assert example_user.username != username

    @pytest.mark.skipif(
        not _is_module_installed("pyhive"), reason="pyhive not installed"
    )
    def test_impersonate_user_presto(self) -> None:
        uri = "presto://localhost"
        principal_user = _GammaUser()
        extra = """
                {
                    "metadata_params": {},
                    "engine_params": {
                               "connect_args":{
                                  "protocol": "https",
                                  "username":"original_user",
                                  "password":"original_user_password"
                               }
                    },
                    "metadata_cache_timeout": {},
                    "schemas_allowed_for_file_upload": []
                }
                """

        with (
            mock.patch("superset.utils.database.create_engine") as mocked_create_engine,
            override_user(principal_user),
        ):
            model = Database(
                database_name="test_database", sqlalchemy_uri=uri, extra=extra
            )
            model.impersonate_user = True
            with model.get_sqla_engine():
                pass
            call_args = mocked_create_engine.call_args

            assert str(call_args[0][0]) == "presto://gamma@localhost/"

            assert call_args[1]["connect_args"] == {
                "protocol": "https",
                "username": "original_user",
                "password": "original_user_password",
                "principal_username": "gamma",
            }

            model.impersonate_user = False
            with model.get_sqla_engine():
                pass
            call_args = mocked_create_engine.call_args

            assert str(call_args[0][0]) == "presto://localhost/"

            assert call_args[1]["connect_args"] == {
                "protocol": "https",
                "username": "original_user",
                "password": "original_user_password",
            }

    @pytest.mark.skipif(
        not _is_module_installed("mysqlclient"), reason="mysqlclient not installed"
    )
    def test_adjust_engine_params_mysql(self) -> None:
        with mock.patch(
            "superset.utils.database.create_engine"
        ) as mocked_create_engine:
            model = Database(
                database_name="test_database1",
                sqlalchemy_uri="mysql://user:password@localhost",
            )
            with model.get_sqla_engine():
                pass
            call_args = mocked_create_engine.call_args

            assert str(call_args[0][0]) == "mysql://user:password@localhost"
            assert call_args[1]["connect_args"]["local_infile"] == 0

            model = Database(
                database_name="test_database2",
                sqlalchemy_uri="mysql+mysqlconnector://user:password@localhost",
            )
            with model.get_sqla_engine():
                pass
            call_args = mocked_create_engine.call_args

            assert (
                str(call_args[0][0]) == "mysql+mysqlconnector://user:password@localhost"
            )
            assert call_args[1]["connect_args"]["allow_local_infile"] == 0

    @pytest.mark.skipif(
        importlib.util.find_spec("trino") is None
        and importlib.util.find_spec("sqlalchemy_trino") is None,
        reason="trino sqlalchemy dialect not installed; URI validation cannot "
        "resolve the trino dialect to build the engine",
    )
    def test_impersonate_user_trino(self) -> None:
        principal_user = _GammaUser()

        with (
            mock.patch("superset.utils.database.create_engine") as mocked_create_engine,
            override_user(principal_user),
        ):
            model = Database(
                database_name="test_database", sqlalchemy_uri="trino://localhost"
            )
            model.impersonate_user = True
            with model.get_sqla_engine():
                pass
            call_args = mocked_create_engine.call_args

            assert str(call_args[0][0]) == "trino://localhost/"
            assert call_args[1]["connect_args"]["user"] == "gamma"

            model = Database(
                database_name="test_database",
                sqlalchemy_uri="trino://original_user:original_user_password@localhost",
            )

            model.impersonate_user = True
            with model.get_sqla_engine():
                pass
            call_args = mocked_create_engine.call_args

            assert (
                str(call_args[0][0])
                == "trino://original_user:original_user_password@localhost/"
            )
            assert call_args[1]["connect_args"]["user"] == "gamma"

    @pytest.mark.skipif(
        not _is_module_installed("pyhive"), reason="pyhive not installed"
    )
    @pytest.mark.skipif(
        not _is_module_installed("thrift"), reason="thrift not installed"
    )
    def test_impersonate_user_hive(self) -> None:
        uri = "hive://localhost"
        principal_user = _GammaUser()
        extra = """
                {
                    "metadata_params": {},
                    "engine_params": {
                               "connect_args":{
                                  "protocol": "https",
                                  "username":"original_user",
                                  "password":"original_user_password"
                               }
                    },
                    "metadata_cache_timeout": {},
                    "schemas_allowed_for_file_upload": []
                }
                """

        with (
            mock.patch("superset.utils.database.create_engine") as mocked_create_engine,
            override_user(principal_user),
        ):
            model = Database(
                database_name="test_database", sqlalchemy_uri=uri, extra=extra
            )
            model.impersonate_user = True
            with model.get_sqla_engine():
                pass
            call_args = mocked_create_engine.call_args

            assert str(call_args[0][0]) == "hive://localhost"

            assert call_args[1]["connect_args"] == {
                "protocol": "https",
                "username": "original_user",
                "password": "original_user_password",
                "configuration": {"hive.server2.proxy.user": "gamma"},
            }

            model.impersonate_user = False
            with model.get_sqla_engine():
                pass
            call_args = mocked_create_engine.call_args

            assert str(call_args[0][0]) == "hive://localhost"

            assert call_args[1]["connect_args"] == {
                "protocol": "https",
                "username": "original_user",
                "password": "original_user_password",
            }

    @pytest.mark.usefixtures("load_energy_table_with_slice")
    @pytest.mark.skipif(
        not _is_module_installed("pyhive"), reason="pyhive not installed"
    )
    def test_select_star(self) -> None:
        db = get_example_database()
        table_name = "energy_usage"
        sql = db.select_star(Table(table_name), show_cols=False, latest_partition=False)
        with db.get_sqla_engine() as engine:
            quote = engine.dialect.identifier_preparer.quote_identifier

        source = quote(table_name) if db.backend in {"presto", "hive"} else table_name
        expected = f"SELECT\n  *\nFROM {source}\nLIMIT 100"
        assert expected in sql
        sql = db.select_star(Table(table_name), show_cols=True, latest_partition=False)
        if db.backend == "presto":
            assert (
                'SELECT\n  "source" AS "source",\n  "target" AS "target",\n  "value" AS "value"\nFROM "energy_usage"\nLIMIT 100'  # noqa: E501
                in sql
            )
        elif db.backend == "hive":
            assert (
                "SELECT\n  `source`,\n  `target`,\n  `value`\nFROM `energy_usage`\nLIMIT 100"  # noqa: E501
                in sql
            )
        else:
            assert (
                "SELECT\n  source,\n  target,\n  value\nFROM energy_usage\nLIMIT 100"
                in sql
            )

    def test_select_star_fully_qualified_names(self) -> None:
        db = get_example_database()
        schema = "schema.name"
        table_name = "table/name"
        sql = db.select_star(
            Table(table_name, schema),
            show_cols=False,
            latest_partition=False,
        )
        fully_qualified_names = {
            "sqlite": '"schema.name"."table/name"',
            "mysql": "`schema.name`.`table/name`",
            "postgres": '"schema.name"."table/name"',
        }
        fully_qualified_name = fully_qualified_names.get(db.db_engine_spec.engine)
        if fully_qualified_name:
            expected = f"SELECT\n  *\nFROM {fully_qualified_name}\nLIMIT 100"
            assert sql.startswith(expected)

    def test_single_statement(self) -> None:
        main_db = get_example_database()

        if main_db.backend == "mysql":
            df = main_db.get_df("SELECT 1", None, None)
            assert df.iat[0, 0] == 1

            df = main_db.get_df("SELECT 1;", None, None)
            assert df.iat[0, 0] == 1

    def test_multi_statement(self) -> None:
        main_db = get_example_database()

        if main_db.backend == "mysql":
            df = main_db.get_df("USE superset; SELECT 1", None, None)
            assert df.iat[0, 0] == 1

            df = main_db.get_df("USE superset; SELECT ';';", None, None)
            assert df.iat[0, 0] == ";"

    def test_get_sqla_engine(self) -> None:
        model = Database(
            database_name="test_database",
            sqlalchemy_uri="mysql://root@localhost",
        )
        model.db_engine_spec.get_dbapi_exception_mapping = mock.Mock(
            return_value={Exception: SupersetException}
        )
        with mock.patch(
            "superset.utils.database.create_engine", side_effect=Exception()
        ):
            with pytest.raises(SupersetException):
                with model.get_sqla_engine():
                    pass


class TestSqlaTableModel:
    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_get_timestamp_expression(self) -> None:
        tbl = _get_table("birth_names")
        ds_col = tbl.get_column("ds")
        sqla_literal = ds_col.get_timestamp_expression(None)
        assert str(sqla_literal.compile()) == "ds"

        sqla_literal = ds_col.get_timestamp_expression("P1D")
        compiled = f"{sqla_literal.compile()}"
        if tbl.database.backend == "mysql":
            assert compiled == "DATE(ds)"

        prev_ds_expr = ds_col.expression
        ds_col.expression = "DATE_ADD(ds, 1)"
        sqla_literal = ds_col.get_timestamp_expression("P1D")
        compiled = f"{sqla_literal.compile()}"
        if tbl.database.backend == "mysql":
            assert compiled == "DATE(DATE_ADD(ds, 1))"
        ds_col.expression = prev_ds_expr

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_get_timestamp_expression_epoch(self) -> None:
        tbl = _get_table("birth_names")
        ds_col = tbl.get_column("ds")

        ds_col.expression = None
        ds_col.python_date_format = "epoch_s"
        sqla_literal = ds_col.get_timestamp_expression(None)
        compiled = f"{sqla_literal.compile()}"
        if tbl.database.backend == "mysql":
            assert compiled == "from_unixtime(ds)"

        ds_col.python_date_format = "epoch_s"
        sqla_literal = ds_col.get_timestamp_expression("P1D")
        compiled = f"{sqla_literal.compile()}"
        if tbl.database.backend == "mysql":
            assert compiled == "DATE(from_unixtime(ds))"

        prev_ds_expr = ds_col.expression
        ds_col.expression = "DATE_ADD(ds, 1)"
        sqla_literal = ds_col.get_timestamp_expression("P1D")
        compiled = f"{sqla_literal.compile()}"
        if tbl.database.backend == "mysql":
            assert compiled == "DATE(from_unixtime(DATE_ADD(ds, 1)))"
        ds_col.expression = prev_ds_expr

    async def query_with_expr_helper(
        self, is_timeseries: bool, inner_join: bool = True
    ) -> Any:
        """Drive the real query-execution pipeline for the expr-groupby cases.

        The port does not implement the legacy synchronous
        ``SqlaTable.query``; the equivalent execution path is the async
        :meth:`SqlaTable.async_query`, which compiles + runs the SQL and
        returns a ``QueryResult`` with the same ``status`` / ``query`` /
        ``df`` surface the original asserts against.
        """
        tbl = _get_table("birth_names")
        ds_col = tbl.get_column("ds")
        ds_col.expression = None
        ds_col.python_date_format = None
        spec = tbl.database.db_engine_spec
        if not spec.allows_joins and inner_join:
            # if the db does not support inner joins, we cannot force it so
            return None
        old_inner_join = spec.allows_joins
        spec.allows_joins = inner_join

        # Use database-specific string concatenation syntax
        arbitrary_gby = (
            "CONCAT(state, gender, '_test')"
            if get_example_database().backend == "mysql"
            else "state || gender || '_test'"
        )

        arbitrary_metric = dict(  # noqa: C408
            label="arbitrary", expressionType="SQL", sqlExpression="SUM(num_boys)"
        )
        query_obj = dict(  # noqa: C408
            groupby=[arbitrary_gby, "name"],
            metrics=[arbitrary_metric],
            filter=[],
            is_timeseries=is_timeseries,
            columns=[],
            granularity="ds",
            from_dttm=None,
            to_dttm=None,
            extras=dict(time_grain_sqla="P1Y"),  # noqa: C408
            series_limit=15 if inner_join and is_timeseries else None,
        )
        qr = await tbl.async_query(query_obj)
        assert qr.status == QueryStatus.SUCCESS
        sql = qr.query
        assert arbitrary_gby in sql
        assert "name" in sql
        if inner_join and is_timeseries:
            assert "JOIN" in sql.upper()
        else:
            assert "JOIN" not in sql.upper()
        spec.allows_joins = old_inner_join
        assert not qr.df.empty
        return qr.df

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_query_with_expr_groupby_timeseries(self) -> None:
        if get_example_database().backend == "presto":
            # TODO(bkyryliuk): make it work for presto.
            return

        def canonicalize_df(df: Any) -> Any:
            ret = df.sort_values(by=list(df.columns.values), inplace=False)
            ret.reset_index(inplace=True, drop=True)
            return ret

        df1 = await self.query_with_expr_helper(is_timeseries=True, inner_join=True)
        name_list1 = canonicalize_df(df1).name.values.tolist()
        df2 = await self.query_with_expr_helper(is_timeseries=True, inner_join=False)
        name_list2 = canonicalize_df(df1).name.values.tolist()
        assert not df2.empty

        assert name_list2 == name_list1

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_query_with_expr_groupby(self) -> None:
        await self.query_with_expr_helper(is_timeseries=False)

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_sql_mutator(self) -> None:
        tbl = _get_table("birth_names")
        query_obj = dict(  # noqa: C408
            groupby=[],
            metrics=None,
            filter=[],
            is_timeseries=False,
            columns=["name"],
            granularity=None,
            from_dttm=None,
            to_dttm=None,
            extras={},
        )
        sql = tbl.get_query_str(query_obj)
        assert "-- COMMENT" not in sql

        def mutator(*args: Any, **kwargs: Any) -> str:
            return "-- COMMENT\n" + args[0]

        prev_mutator = getattr(superset_config, "SQL_QUERY_MUTATOR", None)
        superset_config.SQL_QUERY_MUTATOR = mutator
        try:
            sql = tbl.get_query_str(query_obj)
            assert "-- COMMENT" in sql
        finally:
            superset_config.SQL_QUERY_MUTATOR = prev_mutator

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_sql_mutator_different_params(self) -> None:
        tbl = _get_table("birth_names")
        query_obj = dict(  # noqa: C408
            groupby=[],
            metrics=None,
            filter=[],
            is_timeseries=False,
            columns=["name"],
            granularity=None,
            from_dttm=None,
            to_dttm=None,
            extras={},
        )
        sql = tbl.get_query_str(query_obj)
        assert "-- COMMENT" not in sql

        def mutator(sql: str, database: Any = None, **kwargs: Any) -> str:
            return "-- COMMENT\n--" + "\n" + str(database) + "\n" + sql

        prev_mutator = getattr(superset_config, "SQL_QUERY_MUTATOR", None)
        superset_config.SQL_QUERY_MUTATOR = mutator
        try:
            mutated_sql = tbl.get_query_str(query_obj)
            assert "-- COMMENT" in mutated_sql
            assert tbl.database.name in mutated_sql
        finally:
            superset_config.SQL_QUERY_MUTATOR = prev_mutator

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_query_with_non_existent_metrics(self) -> None:
        tbl = _get_table("birth_names")

        query_obj = dict(  # noqa: C408
            groupby=[],
            metrics=["invalid"],
            filter=[],
            is_timeseries=False,
            columns=["name"],
            granularity=None,
            from_dttm=None,
            to_dttm=None,
            extras={},
        )

        with pytest.raises(Exception) as context:  # noqa: PT011
            tbl.get_query_str(query_obj)

        assert "Metric 'invalid' does not exist" in str(context.value)

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_query_label_without_group_by(self) -> None:
        tbl = _get_table("birth_names")
        query_obj = dict(  # noqa: C408
            groupby=[],
            columns=[
                "gender",
                {
                    "label": "Given Name",
                    "sqlExpression": "name",
                    "expressionType": "SQL",
                },
            ],
            filter=[],
            is_timeseries=False,
            granularity=None,
            from_dttm=None,
            to_dttm=None,
            extras={},
        )

        sql = tbl.get_query_str(query_obj)
        assert re.search('name AS ["`]?Given Name["`]?', sql)

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_data_for_slices_with_no_query_context(self) -> None:
        tbl = _get_table("birth_names")
        session = get_sync_session()
        slc = (
            session.query(Slice)
            .filter_by(
                datasource_id=tbl.id,
                datasource_type="table",
                slice_name="Genders",
            )
            .first()
        )
        data_for_slices = tbl.data_for_slices([slc])
        assert len(data_for_slices["metrics"]) == 1
        assert len(data_for_slices["columns"]) == 1
        assert data_for_slices["metrics"][0]["metric_name"] == "sum__num"
        assert data_for_slices["columns"][0]["column_name"] == "gender"
        assert set(data_for_slices["verbose_map"].keys()) == {
            "__timestamp",
            "sum__num",
            "gender",
        }

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_data_for_slices_with_query_context(self) -> None:
        tbl = _get_table("birth_names")
        session = get_sync_session()
        slc = (
            session.query(Slice)
            .filter_by(
                datasource_id=tbl.id,
                datasource_type="table",
                slice_name="Pivot Table v2",
            )
            .first()
        )
        data_for_slices = tbl.data_for_slices([slc])
        assert len(data_for_slices["metrics"]) == 1
        assert len(data_for_slices["columns"]) == 2
        assert data_for_slices["metrics"][0]["metric_name"] == "sum__num"
        column_names = [col["column_name"] for col in data_for_slices["columns"]]
        assert "name" in column_names
        assert "state" in column_names
        assert set(data_for_slices["verbose_map"].keys()) == {
            "__timestamp",
            "sum__num",
            "name",
            "state",
        }

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_data_for_slices_with_adhoc_column(self) -> None:
        # data_for_slices with an adhoc column on a legacy (form_data) chart
        tbl = _get_table("birth_names")
        slc = Slice(
            slice_name="slice with adhoc column",
            datasource_type="table",
            viz_type="table",
            params=json.dumps(
                {
                    "adhoc_filters": [],
                    "granularity_sqla": "ds",
                    "groupby": [
                        "name",
                        {"label": "adhoc_column", "sqlExpression": "name"},
                    ],
                    "metrics": ["sum__num"],
                    "time_range": "No filter",
                    "viz_type": "table",
                }
            ),
            datasource_id=tbl.id,
        )
        datasource_info = tbl.data_for_slices([slc])
        assert "database" in datasource_info

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_table_column_database(self) -> None:
        tbl = _get_table("birth_names")
        assert tbl.get_column("ds").database is tbl.database
