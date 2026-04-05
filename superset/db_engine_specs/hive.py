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
"""Apache Hive engine spec -- sync/Flask-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/hive.py`` with Flask
imports removed.  Only overridden methods and attributes are included.

Heavy Flask/pyhive-dependent methods (``handle_cursor``, ``df_to_sql``,
``upload_to_s3``) are intentionally omitted.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, TYPE_CHECKING
from urllib import parse

from sqlalchemy import types
from sqlalchemy.engine.url import URL

from superset.constants import TimeGrain
from superset.db_engine_specs.base import BaseEngineSpec

if TYPE_CHECKING:
    from superset.models.core import Database

logger = logging.getLogger(__name__)


class HiveEngineSpec(BaseEngineSpec):
    """Engine spec for Apache Hive."""

    engine = "hive"
    engine_name = "Apache Hive"
    max_column_name_length = 767
    allows_alias_to_source_column = True
    allows_hidden_orderby_agg = False

    supports_dynamic_schema = True

    # When running ``SHOW FUNCTIONS``, what is the name of the column with the
    # function names?
    _show_functions_column = "tab_name"

    _time_grain_expressions = {
        None: "{col}",
        TimeGrain.SECOND: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-dd HH:mm:ss')",  # noqa: E501
        TimeGrain.MINUTE: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-dd HH:mm:00')",  # noqa: E501
        TimeGrain.HOUR: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-dd HH:00:00')",
        TimeGrain.DAY: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-dd 00:00:00')",
        TimeGrain.WEEK: "date_format(date_sub({col}, CAST(7-from_unixtime(unix_timestamp({col}),'u') as int)), 'yyyy-MM-dd 00:00:00')",  # noqa: E501
        TimeGrain.MONTH: "from_unixtime(unix_timestamp({col}), 'yyyy-MM-01 00:00:00')",
        TimeGrain.QUARTER: "date_format(add_months(trunc({col}, 'MM'), -(month({col})-1)%3), 'yyyy-MM-dd 00:00:00')",  # noqa: E501
        TimeGrain.YEAR: "from_unixtime(unix_timestamp({col}), 'yyyy-01-01 00:00:00')",
        TimeGrain.WEEK_ENDING_SATURDAY: "date_format(date_add({col}, INT(6-from_unixtime(unix_timestamp({col}), 'u'))), 'yyyy-MM-dd 00:00:00')",  # noqa: E501
        TimeGrain.WEEK_STARTING_SUNDAY: "date_format(date_add({col}, -INT(from_unixtime(unix_timestamp({col}), 'u'))), 'yyyy-MM-dd 00:00:00')",  # noqa: E501
    }

    # Scoping regex at class level to avoid recompiling
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
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"CAST('{dttm.date().isoformat()}' AS DATE)"
        if isinstance(sqla_type, types.TIMESTAMP):
            return (
                f"""CAST('{dttm.isoformat(sep=" ", timespec="microseconds")}' """
                "AS TIMESTAMP)"
            )
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
    def epoch_to_dttm(cls) -> str:
        return "from_unixtime({col})"

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

        stage_progress = (
            sum(stages.values()) / len(stages.values()) if stages else 0
        )

        progress = (
            100 * (current_job - 1) / total_jobs + stage_progress / total_jobs
        )
        return int(progress)

    @classmethod
    def get_tracking_url_from_logs(cls, log_lines: list[str]) -> str | None:
        lkp = "Tracking URL = "
        for line in log_lines:
            if lkp in line:
                return line.split(lkp)[1]
        return None

    @classmethod
    def has_implicit_cancel(cls) -> bool:
        return True


__all__ = [
    "HiveEngineSpec",
]
