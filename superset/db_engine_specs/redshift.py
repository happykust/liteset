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
"""Amazon Redshift engine spec -- sync/Flask-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/redshift.py`` with Flask
imports removed.  Only overridden methods and attributes are included.
"""

from __future__ import annotations

import logging
import re
from re import Pattern
from typing import Any, TYPE_CHECKING

from superset.db_engine_specs.base import BasicParametersMixin
from superset.db_engine_specs.postgres import PostgresBaseEngineSpec

if TYPE_CHECKING:
    from superset.models.sql_lab import Query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regular expressions to catch custom errors
# ---------------------------------------------------------------------------

CONNECTION_ACCESS_DENIED_REGEX = re.compile(
    'password authentication failed for user "(?P<username>.*?)"'
)
CONNECTION_INVALID_HOSTNAME_REGEX = re.compile(
    'could not translate host name "(?P<hostname>.*?)" to address: '
    "nodename nor servname provided, or not known"
)
CONNECTION_PORT_CLOSED_REGEX = re.compile(
    r"could not connect to server: Connection refused\s+Is the server "
    r'running on host "(?P<hostname>.*?)" (\(.*?\) )?and accepting\s+TCP/IP '
    r"connections on port (?P<port>.*?)\?"
)
CONNECTION_HOST_DOWN_REGEX = re.compile(
    r"could not connect to server: (?P<reason>.*?)\s+Is the server running on "
    r'host "(?P<hostname>.*?)" (\(.*?\) )?and accepting\s+TCP/IP '
    r"connections on port (?P<port>.*?)\?"
)
CONNECTION_UNKNOWN_DATABASE_REGEX = re.compile(
    'database "(?P<database>.*?)" does not exist'
)


class RedshiftEngineSpec(BasicParametersMixin, PostgresBaseEngineSpec):
    engine = "redshift"
    engine_name = "Amazon Redshift"
    max_column_name_length = 127
    default_driver = "psycopg2"

    sqlalchemy_uri_placeholder = (
        "redshift+psycopg2://user:password@host:port/dbname[?key=value&key=value...]"
    )

    encryption_parameters = {"sslmode": "verify-ca"}

    custom_errors: dict[Pattern[str], tuple[str, str, dict[str, Any]]] = {
        CONNECTION_ACCESS_DENIED_REGEX: (
            'Either the username "%(username)s" or the password is incorrect.',
            "CONNECTION_ACCESS_DENIED_ERROR",
            {"invalid": ["username", "password"]},
        ),
        CONNECTION_INVALID_HOSTNAME_REGEX: (
            'The hostname "%(hostname)s" cannot be resolved.',
            "CONNECTION_INVALID_HOSTNAME_ERROR",
            {"invalid": ["host"]},
        ),
        CONNECTION_PORT_CLOSED_REGEX: (
            'Port %(port)s on hostname "%(hostname)s" refused the connection.',
            "CONNECTION_PORT_CLOSED_ERROR",
            {"invalid": ["host", "port"]},
        ),
        CONNECTION_HOST_DOWN_REGEX: (
            'The host "%(hostname)s" might be down, and can\'t be '
            "reached on port %(port)s.",
            "CONNECTION_HOST_DOWN_ERROR",
            {"invalid": ["host", "port"]},
        ),
        CONNECTION_UNKNOWN_DATABASE_REGEX: (
            'We were unable to connect to your database named "%(database)s".'
            " Please verify your database name and try again.",
            "CONNECTION_UNKNOWN_DATABASE_ERROR",
            {"invalid": ["database"]},
        ),
    }

    @staticmethod
    def _mutate_label(label: str) -> str:
        """Redshift only supports lowercase column names and aliases."""
        return label.lower()

    @classmethod
    def get_cancel_query_id(cls, cursor: Any, query: Query) -> str | None:
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
        return row[0]

    @classmethod
    def cancel_query(cls, cursor: Any, query: Query, cancel_query_id: str) -> bool:
        try:
            logger.info("Killing Redshift PID:%s", str(cancel_query_id))
            cursor.execute(
                "SELECT pg_cancel_backend(procpid) "  # noqa: S608
                "FROM pg_stat_activity "
                f"WHERE procpid='{cancel_query_id}'"
            )
            cursor.close()
        except Exception:  # noqa: BLE001
            return False
        return True


__all__ = [
    "RedshiftEngineSpec",
]
