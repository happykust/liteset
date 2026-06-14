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
"""Native Liteset port of the upstream sync DruidEngineSpec unit tests.

Flask-free: no flask, flask_appbuilder or werkzeug. The integration-test
fixtures ``default_db_extra`` / ``ssl_certificate`` are inlined here as plain
string constants to avoid pulling in the Flask app.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from unittest import mock

import pytest
from sqlalchemy import column

from superset.db_engine_specs.druid import DruidEngineSpec as spec  # noqa: N813
from tests.superset.unit.db_engine_specs.utils import assert_convert_dttm

default_db_extra = """{
  "metadata_params": {},
  "engine_params": {},
  "metadata_cache_timeout": {},
  "schemas_allowed_for_file_upload": []
}"""

ssl_certificate = """-----BEGIN CERTIFICATE-----
MIIDnDCCAoQCCQCrdpcNPCA/eDANBgkqhkiG9w0BAQsFADCBjzELMAkGA1UEBhMC
VVMxEzARBgNVBAgMCkNhbGlmb3JuaWExEjAQBgNVBAcMCVNhbiBNYXRlbzEPMA0G
A1UECgwGUHJlc2V0MRMwEQYDVQQLDApTa3Vua3dvcmtzMRIwEAYDVQQDDAlwcmVz
ZXQuaW8xHTAbBgkqhkiG9w0BCQEWDmluZm9AcHJlc2V0LmlvMB4XDTIwMDMyNjEw
NTE1NFoXDTQwMDMyNjEwNTE1NFowgY8xCzAJBgNVBAYTAlVTMRMwEQYDVQQIDApD
YWxpZm9ybmlhMRIwEAYDVQQHDAlTYW4gTWF0ZW8xDzANBgNVBAoMBlByZXNldDET
MBEGA1UECwwKU2t1bmt3b3JrczESMBAGA1UEAwwJcHJlc2V0LmlvMR0wGwYJKoZI
hvcNAQkBFg5pbmZvQHByZXNldC5pbzCCASIwDQYJKoZIhvcNAQEBBQADggEPADCC
AQoCggEBAKNHQZcu2L/6HvZfzy4Hnm3POeztfO+NJ7OzppAcNlLbTAatUk1YoDbJ
5m5GUW8m7pVEHb76UL6Xxei9MoMVvHGuXqQeZZnNd+DySW/227wkOPYOCVSuDsWD
1EReG+pv/z8CDhdwmMTkDTZUDr0BUR/yc8qTCPdZoalj2muDl+k2J3LSCkelx4U/
2iYhoUQD+lzFS3k7ohAfaGc2aZOlwTITopXHSFfuZ7j9muBOYtU7NgpnCl6WgxYP
1+4ddBIauPTBY2gWfZC2FeOfYEqfsUUXRsw1ehEQf4uxxTKNJTfTuVbdgrTYx5QQ
jrM88WvWdyVnIM7u7/x9bawfGX/b/F0CAwEAATANBgkqhkiG9w0BAQsFAAOCAQEA
XYLLk3T5RWIagNa3DPrMI+SjRm4PAI/RsijtBV+9hrkCXOQ1mvlo/ORniaiemHvF
Kh6u6MTl014+f6Ytg/tx/OzuK2ffo9x44ZV/yqkbSmKD1pGftYNqCnBCN0uo1Gzb
HZ+bTozo+9raFN7OGPgbdBmpQT2c+LG5n+7REobHFb7VLeY2/7BKtxNBRXfIxn4X
+MIhpASwLH5X64a1f9LyuPNMyUvKgzDe7jRdX1JZ7uw/1T//OHGQth0jLiapa6FZ
GwgYUaruSZH51ZtxrJSXKSNBA7asPSBbyOmGptLsw2GTAsoBd5sUR4+hbuVo+1ai
XeA3AKTX/OdYWJvr5YIgeQ==
-----END CERTIFICATE-----"""


@pytest.fixture
def dttm() -> datetime:
    return datetime.strptime("2019-01-02 03:04:05.678900", "%Y-%m-%d %H:%M:%S.%f")


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("Date", "CAST(TIME_PARSE('2019-01-02') AS DATE)"),
        ("DateTime", "TIME_PARSE('2019-01-02T03:04:05')"),
        ("TimeStamp", "TIME_PARSE('2019-01-02T03:04:05')"),
        ("UnknownType", None),
    ],
)
def test_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    assert_convert_dttm(spec, target_type, expected_result, dttm)


@pytest.mark.parametrize(
    "time_grain,expected_result",
    [
        ("PT1S", "TIME_FLOOR(CAST(col AS TIMESTAMP), 'PT1S')"),
        ("PT5M", "TIME_FLOOR(CAST({col} AS TIMESTAMP), 'PT5M')"),
        (
            "P1W/1970-01-03T00:00:00Z",
            "TIME_SHIFT(TIME_FLOOR(TIME_SHIFT(CAST(col AS TIMESTAMP), 'P1D', 1), 'P1W'), 'P1D', 5)",  # noqa: E501
        ),
        (
            "1969-12-28T00:00:00Z/P1W",
            "TIME_SHIFT(TIME_FLOOR(TIME_SHIFT(CAST(col AS TIMESTAMP), 'P1D', 1), 'P1W'), 'P1D', -1)",  # noqa: E501
        ),
    ],
)
def test_timegrain_expressions(time_grain: str, expected_result: str) -> None:
    """
    DB Eng Specs (druid): Test time grain expressions
    """
    assert str(
        spec.get_timestamp_expr(col=column("col"), pdf=None, time_grain=time_grain)
    )


def test_extras_without_ssl() -> None:
    database = mock.Mock()
    database.extra = default_db_extra
    database.server_cert = None
    extras = spec.get_extra_params(database)
    assert "connect_args" not in extras["engine_params"]


def test_extras_with_ssl() -> None:
    database = mock.Mock()
    database.extra = default_db_extra
    database.server_cert = ssl_certificate
    extras = spec.get_extra_params(database)
    connect_args = extras["engine_params"]["connect_args"]
    assert connect_args["scheme"] == "https"
    assert "ssl_verify_cert" in connect_args
