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
from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text

from superset.db.engine_specs.postgres import AsyncPostgresEngineSpec

logger = logging.getLogger(__name__)


class AsyncRedshiftEngineSpec(AsyncPostgresEngineSpec):
    """Async engine spec for Amazon Redshift (PostgreSQL wire protocol)."""

    engine = "redshift"
    engine_name = "Amazon Redshift"
    default_driver = "asyncpg"
    max_column_name_length: int = 127

    _custom_errors: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile('password authentication failed for user "(?P<username>.*?)"'),
            'Either the username "{username}" or the password is incorrect.',
        ),
        (
            re.compile(r'could not translate host name "(?P<hostname>.*?)" to address'),
            'The hostname "{hostname}" cannot be resolved.',
        ),
        (
            re.compile(
                r"could not connect to server: Connection refused.*"
                r'host "(?P<hostname>.*?)".*port (?P<port>.*?)\?',
            ),
            'Port {port} on hostname "{hostname}" refused the connection.',
        ),
        (
            re.compile(r'database "(?P<database>.*?)" does not exist'),
            'Unable to connect to database "{database}".',
        ),
    ]

    @classmethod
    async def cancel_query(
        cls,
        conn: AsyncConnection,
        cancel_query_id: str,
    ) -> bool:
        """Cancel query using Redshift's ``pg_cancel_backend(procpid)``."""
        try:
            logger.info("Killing Redshift PID:%s", cancel_query_id)
            await conn.execute(
                text(
                    "SELECT pg_cancel_backend(procpid) "
                    "FROM pg_stat_activity "
                    "WHERE procpid = :pid"
                ),
                {"pid": int(cancel_query_id)},
            )
        except Exception:  # noqa: BLE001
            return False
        return True

    @staticmethod
    def mutate_label(label: str) -> str:
        """Redshift only supports lowercase column names and aliases."""
        return label.lower()
