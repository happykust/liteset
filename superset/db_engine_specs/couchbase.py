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

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from urllib import parse

from sqlalchemy.engine.url import URL

from superset.constants import TimeGrain
from superset.databases.utils import make_url_safe
from superset.db_engine_specs.base import BaseEngineSpec


class CouchbaseEngineSpec(BaseEngineSpec):
    engine = "couchbase"
    engine_aliases = {"couchbasedb"}
    engine_name = "Couchbase"
    default_driver = "couchbase"
    allows_joins = False
    allows_subqueries = False
    sqlalchemy_uri_placeholder = (
        "couchbase://user:password@host[:port]?truststorepath=value?ssl=value"
    )

    _time_grain_expressions = {
        None: "{col}",
        TimeGrain.SECOND: "DATE_TRUNC_STR(TOSTRING({col}),'second')",
        TimeGrain.MINUTE: "DATE_TRUNC_STR(TOSTRING({col}),'minute')",
        TimeGrain.HOUR: "DATE_TRUNC_STR(TOSTRING({col}),'hour')",
        TimeGrain.DAY: "DATE_TRUNC_STR(TOSTRING({col}),'day')",
        TimeGrain.MONTH: "DATE_TRUNC_STR(TOSTRING({col}),'month')",
        TimeGrain.YEAR: "DATE_TRUNC_STR(TOSTRING({col}),'year')",
        TimeGrain.QUARTER: "DATE_TRUNC_STR(TOSTRING({col}),'quarter')",
    }

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "MILLIS_TO_STR({col} * 1000)"

    @classmethod
    def epoch_ms_to_dttm(cls) -> str:
        return "MILLIS_TO_STR({col})"

    @classmethod
    def convert_dttm(
        cls, target_type: str, dttm: datetime, db_extra: Optional[dict[str, Any]] = None
    ) -> Optional[str]:
        if target_type.lower() == "date":
            formatted_date = dttm.date().isoformat()
        else:
            formatted_date = dttm.replace(microsecond=0).isoformat()
        return f"DATETIME(DATE_FORMAT_STR(STR_TO_UTC('{formatted_date}'), 'iso8601'))"

    @classmethod
    def build_sqlalchemy_uri(
        cls,
        parameters: dict[str, Any],
        encrypted_extra: Optional[dict[str, Any]] = None,
    ) -> str:
        query_params = parameters.get("query", {}).copy()
        if parameters.get("encryption"):
            query_params["ssl"] = "true"
        else:
            query_params["ssl"] = "false"

        uri = URL.create(
            "couchbase",
            username=parameters.get("username"),
            password=parameters.get("password"),
            host=parameters["host"],
            port=parameters.get("port"),
            query=query_params,
        )
        return str(uri)

    @classmethod
    def get_parameters_from_uri(
        cls, uri: str, encrypted_extra: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        url = make_url_safe(uri)
        query = {
            key: value
            for key, value in url.query.items()
            if (key, value) not in cls.encryption_parameters.items()
        }
        ssl_value = url.query.get("ssl", "false").lower()
        encryption = ssl_value == "true"
        return {
            "username": url.username,
            "password": url.password,
            "host": url.host,
            "port": url.port,
            "database": url.database,
            "query": query,
            "encryption": encryption,
        }

    @classmethod
    def get_schema_from_engine_params(
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> Optional[str]:
        """
        Return the configured schema.
        """
        return parse.unquote(sqlalchemy_uri.database)
