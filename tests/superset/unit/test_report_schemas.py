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
"""Report schedule detail schema parity with upstream ``show_columns``."""

from __future__ import annotations

import json
from types import SimpleNamespace

import msgspec
import pytest

from superset.schemas.report import (
    ReportDetailResult,
    ReportSchedulePostSchema,
    ReportSchedulePutSchema,
)
from superset.utils import filter_unset

# ---------------------------------------------------------------------------
# Minimal valid POST payload (only required fields).
# ---------------------------------------------------------------------------
_MINIMAL_POST_PAYLOAD = json.dumps(
    {
        "name": "My Alert",
        "type": "Alert",
        "crontab": "0 * * * *",
    }
).encode()


def _decode_post(payload: bytes) -> ReportSchedulePostSchema:
    return msgspec.json.decode(payload, type=ReportSchedulePostSchema)


# ---------------------------------------------------------------------------
# creation_method — POST schema
# ``fields.Enum(required=False)`` with NO allow_none:
#   absent  → not in Marshmallow output dict (server_default='alerts_reports')
#   null    → HTTP 422
# ---------------------------------------------------------------------------


def test_post_creation_method_absent_is_unset() -> None:
    """Absent creation_method must be UNSET, not None.

    Original Marshmallow: absent optional field → not in output dict → DB
    server_default='alerts_reports' applies.  liteset filter_unset strips UNSET
    values, so the DAO must never see creation_method=None and must never write
    NULL to the column.
    """
    schema = _decode_post(_MINIMAL_POST_PAYLOAD)
    assert isinstance(schema.creation_method, msgspec.UnsetType), (
        "creation_method absent must be UNSET (not None) so filter_unset "
        "removes it and the DB server_default applies"
    )
    # filter_unset must strip it — DAO never receives creation_method=None.
    raw = filter_unset(msgspec.structs.asdict(schema))
    assert "creation_method" not in raw


def test_post_creation_method_explicit_null_rejected() -> None:
    """Explicit null for creation_method must be HTTP 422 (DecodeError).

    Original Marshmallow: no allow_none → ValidationError on null input.
    """
    payload = json.dumps(
        {
            "name": "My Alert",
            "type": "Alert",
            "crontab": "0 * * * *",
            "creation_method": None,
        }
    ).encode()
    with pytest.raises(msgspec.DecodeError):
        _decode_post(payload)


def test_post_creation_method_valid_value_passes_through() -> None:
    """Valid creation_method value is preserved and included in filter_unset output."""
    payload = json.dumps(
        {
            "name": "My Alert",
            "type": "Alert",
            "crontab": "0 * * * *",
            "creation_method": "alerts_reports",
        }
    ).encode()
    schema = _decode_post(payload)
    assert schema.creation_method == "alerts_reports"
    raw = filter_unset(msgspec.structs.asdict(schema))
    assert raw["creation_method"] == "alerts_reports"


# ---------------------------------------------------------------------------
# validator_config_json — POST schema
# ``fields.Nested(ValidatorConfigJSONSchema)`` with NO allow_none:
#   absent  → not in Marshmallow output dict (column default='{}' applies)
#   null    → HTTP 422
# ---------------------------------------------------------------------------


def test_post_validator_config_json_absent_is_unset() -> None:
    """Absent validator_config_json must be UNSET, not None.

    Original Marshmallow: absent optional field → not in output dict → SA
    column default='{}' applies.  With None the DAO would write NULL to the
    column and bypass the default, causing json.loads(None) TypeError at
    alert execution time.
    """
    schema = _decode_post(_MINIMAL_POST_PAYLOAD)
    assert isinstance(schema.validator_config_json, msgspec.UnsetType), (
        "validator_config_json absent must be UNSET (not None) so filter_unset "
        "removes it and the DB column default applies"
    )
    # filter_unset must strip it — DAO never receives validator_config_json=None.
    raw = filter_unset(msgspec.structs.asdict(schema))
    assert "validator_config_json" not in raw


def test_post_validator_config_json_explicit_null_rejected() -> None:
    """Explicit null for validator_config_json must be HTTP 422 (DecodeError).

    Original Marshmallow: fields.Nested with no allow_none → ValidationError
    on null input.
    """
    payload = json.dumps(
        {
            "name": "My Alert",
            "type": "Alert",
            "crontab": "0 * * * *",
            "validator_config_json": None,
        }
    ).encode()
    with pytest.raises(msgspec.DecodeError):
        _decode_post(payload)


def test_post_validator_config_json_valid_value_passes_through() -> None:
    """Valid validator_config_json is decoded and included in filter_unset output."""
    payload = json.dumps(
        {
            "name": "My Alert",
            "type": "Alert",
            "crontab": "0 * * * *",
            "validator_config_json": {"op": "==", "threshold": 1.0},
        }
    ).encode()
    schema = _decode_post(payload)
    assert not isinstance(schema.validator_config_json, msgspec.UnsetType)
    assert schema.validator_config_json.op == "=="  # type: ignore[union-attr]
    assert schema.validator_config_json.threshold == 1.0  # type: ignore[union-attr]
    raw = filter_unset(msgspec.structs.asdict(schema))
    assert "validator_config_json" in raw


# ---------------------------------------------------------------------------
# ReportSchedulePutSchema — validator_config_json null rejection
# (fields.Nested with NO allow_none)
# ---------------------------------------------------------------------------


def _decode_put(payload: bytes) -> ReportSchedulePutSchema:
    return msgspec.json.decode(payload, type=ReportSchedulePutSchema)


def test_put_validator_config_json_absent_is_unset() -> None:
    """Absent validator_config_json in PUT must be UNSET (not None).

    Original Marshmallow PUT schema: ``fields.Nested(ValidatorConfigJSONSchema,
    required=False)`` with no allow_none → absent field is simply absent from
    the deserialized dict; the DAO never touches the column.
    """
    schema = _decode_put(b"{}")
    assert isinstance(schema.validator_config_json, msgspec.UnsetType), (
        "validator_config_json absent in PUT must be UNSET, not None"
    )
    raw = filter_unset(msgspec.structs.asdict(schema))
    assert "validator_config_json" not in raw


def test_put_validator_config_json_explicit_null_rejected() -> None:
    """Explicit null for validator_config_json in PUT must be rejected (422).

    Original Marshmallow: fields.Nested with no allow_none rejects null with
    HTTP 422.  If null were accepted it would reach the update command, be
    silently skipped by the ``is not None`` guard, write NULL to the column,
    and cause ``json.loads(None) → TypeError`` during alert execution.
    """
    payload = json.dumps({"validator_config_json": None}).encode()
    with pytest.raises(msgspec.DecodeError):
        _decode_put(payload)


def test_put_validator_config_json_valid_value_passes_through() -> None:
    """Valid validator_config_json object in PUT is decoded and kept."""
    payload = json.dumps(
        {"validator_config_json": {"op": ">", "threshold": 0.5}}
    ).encode()
    schema = _decode_put(payload)
    assert not isinstance(schema.validator_config_json, msgspec.UnsetType)
    assert schema.validator_config_json.op == ">"  # type: ignore[union-attr]
    raw = filter_unset(msgspec.structs.asdict(schema))
    assert "validator_config_json" in raw


# ---------------------------------------------------------------------------
# ReportSchedulePutSchema — extra null rejection
# (fields.Dict(dump_default=None) with NO allow_none; dump_default only
# affects serialisation, not loading)
# ---------------------------------------------------------------------------


def test_put_extra_absent_is_unset() -> None:
    """Absent extra in PUT must be UNSET (not None).

    Original Marshmallow PUT schema: ``fields.Dict(dump_default=None)`` with no
    allow_none → absent field is simply absent from the deserialized dict; the
    DAO never calls the ExtraJSONMixin setter.
    """
    schema = _decode_put(b"{}")
    assert isinstance(schema.extra, msgspec.UnsetType), (
        "extra absent in PUT must be UNSET, not None"
    )
    raw = filter_unset(msgspec.structs.asdict(schema))
    assert "extra" not in raw


def test_put_extra_explicit_null_rejected() -> None:
    """Explicit null for extra in PUT must be rejected (422).

    Original Marshmallow: fields.Dict with no allow_none rejects null with
    HTTP 422.  If null were accepted it would reach the DAO setter, call
    ``json.dumps(None)`` which writes the string ``"null"`` to the JSON column,
    and return ``{}`` on the next read — silent data corruption.
    """
    payload = json.dumps({"extra": None}).encode()
    with pytest.raises(msgspec.DecodeError):
        _decode_put(payload)


def test_put_extra_valid_value_passes_through() -> None:
    """Valid extra dict in PUT is decoded and kept."""
    payload = json.dumps({"extra": {"dashboard": {"expandedSlices": {}}}}).encode()
    schema = _decode_put(payload)
    assert not isinstance(schema.extra, msgspec.UnsetType)
    raw = filter_unset(msgspec.structs.asdict(schema))
    assert "extra" in raw
    assert raw["extra"] == {"dashboard": {"expandedSlices": {}}}


# ---------------------------------------------------------------------------
# ReportDetailResult — show_columns parity
# ---------------------------------------------------------------------------


def test_report_detail_includes_id() -> None:
    """``id`` is in upstream show_columns -> must appear inside ``result``.

    The Alerts/Reports edit modal reads it from the detail response.
    """
    obj = SimpleNamespace(id=17, name="my report", type="Report")
    result = ReportDetailResult.from_model(obj)
    assert result.id == 17
    assert result.name == "my report"
    assert result.type == "Report"
    # Relationship fields default cleanly when absent on the model.
    assert result.owners == []
    assert result.recipients == []
    assert result.chart is None
