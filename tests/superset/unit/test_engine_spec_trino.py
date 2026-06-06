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
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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


# ---------------------------------------------------------------------------
# Sync TrinoEngineSpec — candidate 2: get_extra_params user-agent/source
# ---------------------------------------------------------------------------


def test_sync_trino_get_extra_params_source_user_agent() -> None:
    """get_extra_params must set connect_args['source'] to the user-agent string
    (1:1 with superset_old; gap was: source key was simply missing in the port).
    """
    from superset.db_engine_specs.trino import TrinoEngineSpec

    database = MagicMock()
    database.extra = "{}"
    database.server_cert = None

    with patch(
        "superset.utils.core.get_user_agent", return_value="Apache Superset"
    ):
        extra = TrinoEngineSpec.get_extra_params(database)

    connect_args = extra.get("engine_params", {}).get("connect_args", {})
    assert connect_args.get("source") == "Apache Superset"


def test_sync_trino_get_extra_params_source_not_overwritten() -> None:
    """setdefault('source', ...) must NOT overwrite a pre-existing source value."""
    from superset.db_engine_specs.trino import TrinoEngineSpec
    from superset.utils import json

    database = MagicMock()
    database.extra = json.dumps(
        {"engine_params": {"connect_args": {"source": "custom-agent"}}}
    )
    database.server_cert = None

    with patch(
        "superset.utils.core.get_user_agent", return_value="Apache Superset"
    ):
        extra = TrinoEngineSpec.get_extra_params(database)

    connect_args = extra.get("engine_params", {}).get("connect_args", {})
    assert connect_args.get("source") == "custom-agent"


# ---------------------------------------------------------------------------
# Sync TrinoEngineSpec — candidate 1: update_params_from_encrypted_extra
#                        ALLOWED_EXTRA_AUTHENTICATIONS custom-auth support
# ---------------------------------------------------------------------------


def test_sync_trino_custom_auth_allowed() -> None:
    """Custom auth method present in ALLOWED_EXTRA_AUTHENTICATIONS must be used.
    (gap was: the else-branch raised ValueError instead of consulting the config).
    """
    from superset.db_engine_specs.trino import TrinoEngineSpec
    from superset.utils import json

    auth_class = MagicMock()
    auth_params = {"params1": "v1", "params2": "v2"}

    database = MagicMock()
    database.encrypted_extra = json.dumps(
        {"auth_method": "custom_auth", "auth_params": auth_params}
    )

    mock_settings = MagicMock()
    mock_settings.allowed_extra_authentications = {
        "trino": {"custom_auth": auth_class}
    }

    with patch("superset.config.SupersetSettings", return_value=mock_settings):
        params: dict[str, Any] = {}
        TrinoEngineSpec.update_params_from_encrypted_extra(database, params)

    connect_args = params.get("connect_args", {})
    assert connect_args.get("http_scheme") == "https"
    auth_class.assert_called_once_with(**auth_params)


def test_sync_trino_custom_auth_denied() -> None:
    """Custom auth method NOT in ALLOWED_EXTRA_AUTHENTICATIONS must raise ValueError
    with the exact upstream error message.
    """
    from superset.db_engine_specs.trino import TrinoEngineSpec
    from superset.utils import json

    auth_method = "my.module:TrinoAuthClass"
    database = MagicMock()
    database.encrypted_extra = json.dumps(
        {"auth_method": auth_method, "auth_params": {}}
    )

    mock_settings = MagicMock()
    mock_settings.allowed_extra_authentications = {"trino": {}}

    with patch("superset.config.SupersetSettings", return_value=mock_settings):
        with pytest.raises(ValueError, match="must be listed in 'ALLOWED_EXTRA_AUTHENTICATIONS' config"):
            TrinoEngineSpec.update_params_from_encrypted_extra(database, {})
