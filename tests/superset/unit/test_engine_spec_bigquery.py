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
"""Unit tests for BigQueryEngineSpec (sync/Flask-compatible port)."""

from __future__ import annotations


def test_parameters_json_schema_credentials_info_no_nullable() -> None:
    """
    BIGQUERY_PARAMETERS_JSON_SCHEMA must NOT include a ``nullable`` key for
    ``credentials_info``.

    The original BigQueryParametersSchema uses
    ``EncryptedString(required=False, metadata={...})`` without
    ``allow_none=True``, so the APISpec/Marshmallow pipeline never emits
    ``nullable: true``.  A hardcoded ``"nullable": True`` in the liteset
    schema dict diverges from the original GET /api/v1/database/available/
    response body.
    """
    from superset.db_engine_specs.bigquery import BIGQUERY_PARAMETERS_JSON_SCHEMA

    credentials_prop = BIGQUERY_PARAMETERS_JSON_SCHEMA["properties"]["credentials_info"]
    assert "nullable" not in credentials_prop, (
        "credentials_info must not contain 'nullable' — the original Marshmallow "
        "EncryptedString(required=False) schema never emits this key"
    )


def test_parameters_json_schema_credentials_info_shape() -> None:
    """
    The credentials_info property in BIGQUERY_PARAMETERS_JSON_SCHEMA must match
    the OpenAPI shape produced by the original Marshmallow schema:
    ``type: string``, ``description``, and ``x-encrypted-extra: true`` only.
    """
    from superset.db_engine_specs.bigquery import BIGQUERY_PARAMETERS_JSON_SCHEMA

    credentials_prop = BIGQUERY_PARAMETERS_JSON_SCHEMA["properties"]["credentials_info"]
    assert credentials_prop["type"] == "string"
    assert credentials_prop.get("x-encrypted-extra") is True
    assert "description" in credentials_prop
    # Confirm no extra keys beyond the expected three
    allowed = {"type", "description", "x-encrypted-extra"}
    extra_keys = set(credentials_prop.keys()) - allowed
    assert extra_keys == set(), f"Unexpected keys in credentials_info: {extra_keys}"


def test_parameters_json_schema_structure() -> None:
    """
    Top-level BIGQUERY_PARAMETERS_JSON_SCHEMA must be an object with
    ``credentials_info`` and ``query`` properties — no ``required`` array
    (both fields are optional in the original).
    """
    from superset.db_engine_specs.bigquery import BIGQUERY_PARAMETERS_JSON_SCHEMA

    assert BIGQUERY_PARAMETERS_JSON_SCHEMA["type"] == "object"
    props = BIGQUERY_PARAMETERS_JSON_SCHEMA["properties"]
    assert "credentials_info" in props
    assert "query" in props
    assert props["query"]["type"] == "object"
