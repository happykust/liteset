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
from superset.db.engine_specs.trino import AsyncTrinoEngineSpec


def test_trino_engine_attributes():
    assert AsyncTrinoEngineSpec.engine == "trino"
    assert AsyncTrinoEngineSpec.engine_name == "Trino"
    assert AsyncTrinoEngineSpec.default_driver == "aiotrino"


def test_trino_time_grains():
    grains = AsyncTrinoEngineSpec.get_time_grain_expressions()
    assert None in grains
    assert "P1D" in grains
    assert "DATE_TRUNC" in grains["P1D"]


def test_trino_custom_errors():
    errors = AsyncTrinoEngineSpec.extract_errors(
        Exception("line 5:10: Column 'x' cannot be resolved")
    )
    assert len(errors) >= 1
    assert "x" in errors[0]["message"]


def test_trino_table_not_found_error():
    errors = AsyncTrinoEngineSpec.extract_errors(
        Exception("Table 'mydb.schema.users' does not exist")
    )
    assert len(errors) >= 1


def test_trino_adjust_engine_params():
    uri, args = AsyncTrinoEngineSpec.adjust_engine_params("trino://host:8080/catalog")
    assert args["http_scheme"] == "https"


def test_trino_adjust_engine_params_preserves():
    uri, args = AsyncTrinoEngineSpec.adjust_engine_params(
        "trino://host:8080/catalog",
        connect_args={"http_scheme": "http"},
    )
    assert args["http_scheme"] == "http"


def test_trino_fallback_error():
    errors = AsyncTrinoEngineSpec.extract_errors(RuntimeError("something unknown"))
    assert errors[0]["message"] == "something unknown"
    assert errors[0]["error_type"] == "RuntimeError"
