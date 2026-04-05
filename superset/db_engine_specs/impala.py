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
"""Apache Impala engine spec -- sync/Flask-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/impala.py`` with Flask
imports removed.  Only overridden methods and attributes are included.

Heavy Flask-dependent methods (``handle_cursor``, ``execute``) are
intentionally omitted.
"""

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
    """Engine spec for Cloudera's Impala"""

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
