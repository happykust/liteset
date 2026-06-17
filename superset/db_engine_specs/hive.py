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
# mypy: ignore-errors
"""Apache Hive database engine spec."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, TYPE_CHECKING
from urllib import parse

from sqlalchemy import Column, types
from sqlalchemy.engine.base import Engine
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import URL
from sqlalchemy.sql.expression import ColumnClause, Select

from superset.common.query_status import QueryStatus
from superset.constants import TimeGrain
from superset.db_engine_specs.base import BaseEngineSpec, ResultSetColumnType
from superset.db_engine_specs.presto import PrestoEngineSpec
from superset.sql.parse import Table

if TYPE_CHECKING:
    from superset.models.core import Database
    from superset.models.sql_lab import Query

logger = logging.getLogger(__name__)


class HiveEngineSpec(PrestoEngineSpec):
    engine = "hive"
    engine_name = "Apache Hive"
    max_column_name_length = 767
    allows_alias_to_source_column = True
    allows_hidden_orderby_agg = False

    supports_dynamic_schema = True
    supports_cross_catalog_queries = False

    _show_functions_column = "tab_name"

    # pylint: disable=line-too-long
    _time_grain_expressions = {
        None: "{col}",
        TimeGrain.SECOND: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-dd HH:mm:ss')",
        TimeGrain.MINUTE: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-dd HH:mm:00')",
        TimeGrain.HOUR: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-dd HH:00:00')",
        TimeGrain.DAY: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-dd 00:00:00')",
        TimeGrain.WEEK: "date_format(date_sub({col}, CAST(7-from_unixtime(unix_timestamp({col}),'u') as int)), 'yyyy-MM-dd 00:00:00')",  # noqa: E501
        TimeGrain.MONTH: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-01 00:00:00')",
        TimeGrain.QUARTER: "date_format(add_months(trunc({col}, 'MM'), -(month({col})-1)%3), 'yyyy-MM-dd 00:00:00')",  # noqa: E501
        TimeGrain.YEAR: "from_unixtime(unix_timestamp({col}), 'yyyy-01-01 00:00:00')",
        TimeGrain.WEEK_ENDING_SATURDAY: "date_format(date_add({col}, INT(6-from_unixtime(unix_timestamp({col}), 'u'))), 'yyyy-MM-dd 00:00:00')",  # noqa: E501
        TimeGrain.WEEK_STARTING_SUNDAY: "date_format(date_add({col}, -INT(from_unixtime(unix_timestamp({col}), 'u'))), 'yyyy-MM-dd 00:00:00')",  # noqa: E501
    }

    jobs_stats_r = re.compile(r".*INFO.*Total jobs = (?P<max_jobs>[0-9]+)")
    launching_job_r = re.compile(
        ".*INFO.*Launching Job (?P<job_number>[0-9]+) out of (?P<max_jobs>[0-9]+)"
    )
    stage_progress_r = re.compile(
        r".*INFO.*Stage-(?P<stage_number>[0-9]+).*"
        r"map = (?P<map_progress>[0-9]+)%.*"
        r"reduce = (?P<reduce_progress>[0-9]+)%.*"
    )

    @classmethod
    def patch(cls) -> None:
        # pylint: disable=import-outside-toplevel
        from pyhive import hive
        from TCLIService import (
            constants as patched_constants,
            TCLIService as patched_TCLIService,
            ttypes as patched_ttypes,
        )

        hive.TCLIService = patched_TCLIService
        hive.constants = patched_constants
        hive.ttypes = patched_ttypes

    @classmethod
    def fetch_data(cls, cursor: Any, limit: int | None = None) -> list[tuple[Any, ...]]:
        # pylint: disable=import-outside-toplevel
        import pyhive
        from TCLIService import ttypes

        state = cursor.poll()
        if state.operationState == ttypes.TOperationState.ERROR_STATE:
            raise Exception(  # pylint: disable=broad-exception-raised
                "Query error", state.errorMessage
            )
        try:
            return super().fetch_data(cursor, limit)
        except pyhive.exc.ProgrammingError:
            return []

    @classmethod
    def convert_dttm(
        cls, target_type: str, dttm: datetime, db_extra: dict[str, Any] | None = None
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"CAST('{dttm.date().isoformat()}' AS DATE)"
        if isinstance(sqla_type, types.TIMESTAMP):
            return f"""CAST('{
                dttm.isoformat(sep=" ", timespec="microseconds")
            }' AS TIMESTAMP)"""
        return None

    @classmethod
    def adjust_engine_params(
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        if schema:
            uri = uri.set(database=parse.quote(schema, safe=""))

        return uri, connect_args

    @classmethod
    def get_schema_from_engine_params(
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> str | None:
        return parse.unquote(sqlalchemy_uri.database)

    @classmethod
    def _extract_error_message(cls, ex: Exception) -> str:
        msg = str(ex)
        match = re.search(r'errorMessage="(.*?)(?<!\\)"', msg)
        if match:
            msg = match.group(1)
        return msg

    @classmethod
    def progress(cls, log_lines: list[str]) -> int:
        total_jobs = 1  # assuming there's at least 1 job
        current_job = 1
        stages: dict[int, float] = {}
        for line in log_lines:
            match = cls.jobs_stats_r.match(line)
            if match:
                total_jobs = int(match.groupdict()["max_jobs"]) or 1
            match = cls.launching_job_r.match(line)
            if match:
                current_job = int(match.groupdict()["job_number"])
                total_jobs = int(match.groupdict()["max_jobs"]) or 1
                stages = {}
            match = cls.stage_progress_r.match(line)
            if match:
                stage_number = int(match.groupdict()["stage_number"])
                map_progress = int(match.groupdict()["map_progress"])
                reduce_progress = int(match.groupdict()["reduce_progress"])
                stages[stage_number] = (map_progress + reduce_progress) / 2
        logger.info(
            "Progress detail: %s, current job %s, total jobs: %s",
            stages,
            current_job,
            total_jobs,
        )

        stage_progress = sum(stages.values()) / len(stages.values()) if stages else 0

        progress = 100 * (current_job - 1) / total_jobs + stage_progress / total_jobs
        return int(progress)

    @classmethod
    def get_tracking_url_from_logs(cls, log_lines: list[str]) -> str | None:
        lkp = "Tracking URL = "
        for line in log_lines:
            if lkp in line:
                return line.split(lkp)[1]
        return None

    @classmethod
    def handle_cursor(  # pylint: disable=too-many-locals  # noqa: C901
        cls, cursor: Any, query: Query
    ) -> None:
        # pylint: disable=import-outside-toplevel
        from pyhive import hive
        from sqlalchemy.orm import object_session

        from superset.config import SupersetSettings

        unfinished_states = (
            hive.ttypes.TOperationState.INITIALIZED_STATE,
            hive.ttypes.TOperationState.RUNNING_STATE,
        )
        polled = cursor.poll()
        last_log_line = 0
        tracking_url = None
        job_id = None
        query_id = query.id
        session = object_session(query)
        while polled.operationState in unfinished_states:
            if session is not None:
                session.refresh(query)
                query = session.query(type(query)).filter_by(id=query_id).one()
            if query.status == QueryStatus.STOPPED:
                cursor.cancel()
                break

            try:
                logs = cursor.fetch_logs()
                log = "\n".join(logs) if logs else ""
            except Exception:  # pylint: disable=broad-except
                logger.warning("Call to GetLog() failed")
                log = ""

            if log:
                log_lines = log.splitlines()
                progress = cls.progress(log_lines)
                logger.info(
                    "Query %s: Progress total: %s", str(query_id), str(progress)
                )
                needs_commit = False
                if progress > query.progress:
                    query.progress = progress
                    needs_commit = True
                if not tracking_url:
                    tracking_url = cls.get_tracking_url_from_logs(log_lines)
                    if tracking_url:
                        job_id = tracking_url.split("/")[-2]
                        logger.info(
                            "Query %s: Found the tracking url: %s",
                            str(query_id),
                            tracking_url,
                        )
                        query.tracking_url_raw = tracking_url
                        logger.info("Query %s: Job id: %s", str(query_id), str(job_id))
                        needs_commit = True
                if job_id and len(log_lines) > last_log_line:
                    for l in log_lines[last_log_line:]:  # noqa: E741
                        logger.info("Query %s: [%s] %s", str(query_id), str(job_id), l)
                    last_log_line = len(log_lines)
                if needs_commit and session is not None:
                    session.commit()  # pylint: disable=consider-using-transaction
            sleep_interval = SupersetSettings().db_poll_interval_seconds.get(
                cls.engine, 5
            )
            time.sleep(sleep_interval)
            polled = cursor.poll()

    @classmethod
    def get_columns(
        cls,
        inspector: Inspector,
        table: Table,
        options: dict[str, Any] | None = None,
    ) -> list[ResultSetColumnType]:
        return BaseEngineSpec.get_columns(inspector, table, options)

    @classmethod
    def where_latest_partition(
        cls,
        database: Database,
        table: Table,
        query: Select,
        columns: list[ResultSetColumnType] | None = None,
    ) -> Select | None:
        try:
            col_names, values = cls.latest_partition(
                database,
                table,
                show_first=True,
            )
        except Exception:  # pylint: disable=broad-except
            # table is not partitioned
            return None
        if values is not None and columns is not None:
            for col_name, value in zip(col_names, values, strict=False):
                for clm in columns:
                    if clm.get("name") == col_name:
                        query = query.where(Column(col_name) == value)

            return query
        return None

    @classmethod
    def _get_fields(cls, cols: list[ResultSetColumnType]) -> list[ColumnClause]:
        return BaseEngineSpec._get_fields(cols)  # pylint: disable=protected-access

    @classmethod
    def latest_sub_partition(  # type: ignore
        cls,
        database: Database,
        table: Table,
        **kwargs: Any,
    ) -> str:
        # TODO(bogdan): implement`
        pass

    @classmethod
    def _latest_partition_from_df(cls, df: Any) -> list[str] | None:
        # Hive partitions look like ds={partition name}/ds={partition name}
        if not df.empty:
            return [
                partition_str.split("=")[1]
                for partition_str in df.iloc[:, 0].max().split("/")
            ]
        return None

    @classmethod
    def _partition_query(  # pylint: disable=all
        cls,
        table: Table,
        indexes: list[dict[str, Any]],
        database: Database,
        limit: int = 0,
        order_by: list[tuple[str, bool]] | None = None,
        filters: dict[Any, Any] | None = None,
    ) -> str:
        full_table_name = (
            f"{table.schema}.{table.table}" if table.schema else table.table
        )
        return f"SHOW PARTITIONS {full_table_name}"

    @classmethod
    def select_star(  # pylint: disable=all
        cls,
        database: Database,
        table: Table,
        engine: Engine,
        limit: int = 100,
        show_cols: bool = False,
        indent: bool = True,
        latest_partition: bool = True,
        cols: list[ResultSetColumnType] | None = None,
    ) -> str:
        # remove catalog from table name if it exists
        table = Table(table.table, table.schema, None)

        return super(PrestoEngineSpec, cls).select_star(
            database,
            table,
            engine,
            limit,
            show_cols,
            indent,
            latest_partition,
            cols,
        )

    @classmethod
    def impersonate_user(
        cls,
        database: Database,
        username: str | None,
        user_token: str | None,
        url: URL,
        engine_kwargs: dict[str, Any],
    ) -> tuple[URL, dict[str, Any]]:
        if username is None:
            return url, engine_kwargs

        backend_name = url.get_backend_name()
        connect_args = engine_kwargs.setdefault("connect_args", {})
        if backend_name == "hive":
            configuration = connect_args.get("configuration", {})
            configuration["hive.server2.proxy.user"] = username
            connect_args["configuration"] = configuration

        return url, engine_kwargs

    @staticmethod
    def execute(  # type: ignore
        cursor,
        query: str,
        database: Database,
        async_: bool = False,
    ):  # pylint: disable=arguments-differ
        kwargs = {"async": async_}
        cursor.execute(query, **kwargs)

    @classmethod
    def get_function_names(cls, database: Database) -> list[str]:
        """Results cached per-database via sync_cache (manual get/set);
        no upstream memoize shim available."""
        from superset.extensions import cache_manager

        cache_key = f"db:{getattr(database, 'id', None)}:hive:function_names"
        try:
            cached = cache_manager.sync_cache.get(cache_key)
            if cached is not None:
                return cached
        except Exception:  # noqa: BLE001
            logger.debug("function-name cache read failed", exc_info=True)

        names = cls._fetch_function_names(database)

        try:
            cache_manager.sync_cache.set(cache_key, names)
        except Exception:  # noqa: BLE001
            logger.debug("function-name cache write failed", exc_info=True)
        return names

    @classmethod
    def _fetch_function_names(cls, database: Database) -> list[str]:
        df = database.get_df("SHOW FUNCTIONS")
        if cls._show_functions_column in df:
            return df[cls._show_functions_column].tolist()

        columns = df.columns.values.tolist()
        logger.error(
            "Payload from `SHOW FUNCTIONS` has the incorrect format. "
            "Expected column `%s`, found: %s.",
            cls._show_functions_column,
            ", ".join(columns),
            exc_info=True,
        )
        if len(columns) == 1:
            return df[columns[0]].tolist()

        return []

    @classmethod
    def has_implicit_cancel(cls) -> bool:
        return True

    @classmethod
    def get_view_names(
        cls,
        database: Database,
        inspector: Inspector,
        schema: str | None,
    ) -> set[str]:
        """
        Get all the view names within the specified schema.

        Per the SQLAlchemy definition if the schema is omitted the database’s default
        schema is used, however some dialects infer the request as schema agnostic.

        Note that PyHive's Hive SQLAlchemy dialect does not adhere to the specification
        where the `get_view_names` method returns both real tables and views. Futhermore
        the dialect wrongfully infers the request as schema agnostic when the schema is
        omitted.

        :param database: The database to inspect
        :param inspector: The SQLAlchemy inspector
        :param schema: The schema to inspect
        :returns: The view names
        """

        sql = "SHOW VIEWS"

        if schema:
            sql += f" IN `{schema}`"

        with cls._hive_raw_connection(database, schema=schema) as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            results = cursor.fetchall()
            return {row[0] for row in results}

    @classmethod
    def _hive_raw_connection(
        cls,
        database: Database,
        schema: str | None = None,
    ) -> Any:
        """Reuses the Presto port's ``_raw_connection`` helper
        (sync engine + prequeries)."""
        from superset.db_engine_specs.presto import _raw_connection

        return _raw_connection(database, schema=schema)


__all__ = [
    "HiveEngineSpec",
]
