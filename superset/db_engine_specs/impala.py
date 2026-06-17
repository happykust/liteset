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
"""Apache Impala database engine spec."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import types
from sqlalchemy.engine.reflection import Inspector

from superset.constants import TimeGrain
from superset.db_engine_specs.base import BaseEngineSpec

if TYPE_CHECKING:
    from superset.models.sql_lab import Query

logger = logging.getLogger(__name__)

QUERY_PROGRESS_REGEX = re.compile(r"Query.*: (?P<query_progress>[0-9]+)%")


class ImpalaEngineSpec(BaseEngineSpec):
    engine = "impala"
    engine_name = "Apache Impala"

    _time_grain_expressions = {
        None: "{col}",
        TimeGrain.MINUTE: "TRUNC({col}, 'MI')",
        TimeGrain.HOUR: "TRUNC({col}, 'HH')",
        TimeGrain.DAY: "TRUNC({col}, 'DD')",
        TimeGrain.WEEK: "TRUNC({col}, 'WW')",
        TimeGrain.MONTH: "TRUNC({col}, 'MONTH')",
        TimeGrain.QUARTER: "TRUNC({col}, 'Q')",
        TimeGrain.YEAR: "TRUNC({col}, 'YYYY')",
    }

    # Impala only exposes the cancel-query id after execute() begins, so
    # execute_with_cursor must fetch it post-execute (upstream impala.py:61).
    has_query_id_before_execute = False

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "from_unixtime({col})"

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
            return f"""CAST('{dttm.isoformat(timespec="microseconds")}' AS TIMESTAMP)"""
        return None

    @classmethod
    def get_schema_names(cls, inspector: Inspector) -> set[str]:
        return {
            row[0]
            for row in inspector.engine.execute("SHOW SCHEMAS")
            if not row[0].startswith("_")
        }

    @classmethod
    def has_implicit_cancel(cls) -> bool:
        return False

    @classmethod
    def execute(
        cls,
        cursor: Any,
        query: str,
        database: Any,
        **kwargs: Any,
    ) -> None:
        try:
            cursor.execute_async(query)
        except Exception as ex:
            raise cls.get_dbapi_mapped_exception(ex) from ex

    @classmethod
    def handle_cursor(cls, cursor: Any, query: Query) -> None:
        # pylint: disable=import-outside-toplevel
        import time

        from sqlalchemy.orm import object_session

        from superset.config import SupersetSettings
        from superset.constants import QUERY_EARLY_CANCEL_KEY

        query_id = query.id
        unfinished_states = (
            "INITIALIZED_STATE",
            "RUNNING_STATE",
        )

        session = object_session(query)
        try:
            status = cursor.status()
            while status in unfinished_states:
                if session is not None:
                    session.refresh(query)
                    query = session.query(type(query)).filter_by(id=query_id).one()
                if query.extra.get(QUERY_EARLY_CANCEL_KEY):
                    cursor.cancel_operation()
                    cursor.close_operation()
                    cursor.close()
                    break

                try:
                    log = cursor.get_log() or ""
                except Exception:  # pylint: disable=broad-except  # noqa: BLE001
                    logger.warning("Call to GetLog() failed")
                    log = ""

                if log:
                    match = QUERY_PROGRESS_REGEX.match(log)
                    if match:
                        progress = int(match.groupdict()["query_progress"])
                        logger.debug(
                            "Query %s: Progress total: %s",
                            str(query_id),
                            str(progress),
                        )
                        if progress > query.progress and session is not None:
                            query.progress = progress
                            session.commit()
                sleep_interval = SupersetSettings().db_poll_interval_seconds.get(
                    cls.engine, 5
                )
                time.sleep(sleep_interval)
                status = cursor.status()
        except Exception:  # pylint: disable=broad-except  # noqa: BLE001
            logger.debug("Call to status() failed ")
            return

    @classmethod
    def get_cancel_query_id(cls, cursor: Any, query: Query) -> str | None:
        last_operation = getattr(cursor, "_last_operation", None)
        if not last_operation:
            return None
        guid = last_operation.handle.operationId.guid[::-1].hex()
        return f"{guid[-16:]}:{guid[:16]}"

    @classmethod
    def cancel_query(cls, cursor: Any, query: Query, cancel_query_id: str) -> bool:
        try:
            import requests

            impala_host = query.database.url_object.host
            url = f"http://{impala_host}:25000/cancel_query?query_id={cancel_query_id}"
            response = requests.post(url, timeout=3)
        except Exception:  # noqa: BLE001
            return False

        return bool(response and response.status_code == 200)


__all__ = [
    "ImpalaEngineSpec",
]
