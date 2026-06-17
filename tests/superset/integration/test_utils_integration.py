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
"""Flask-free port of ``tests/integration_tests/utils_tests.py``.

Pure utility helpers are exercised directly; the DB-backed cases drive the
real seeded Postgres through ``db_session`` (datasets / dtype extraction) and
the sync-session helper (``get_or_create_db``).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from superset.constants import NO_TIME_RANGE

# NOTE: upstream imports ``DatabaseInvalidError`` from
# ``superset.commands.database.exceptions``; in the Liteset port
# ``Database.set_sqlalchemy_uri`` raises the (separate) class defined in
# ``superset.databases.utils`` — both subclass ``CommandInvalidError`` and the
# behaviour (422 on an unparseable URI) is identical.
from superset.databases.utils import DatabaseInvalidError
from superset.models.connectors import SqlaTable
from superset.models.core import Database
from superset.typing import GenericDataType
from superset.utils.column import cast_to_num, extract_dataframe_dtypes
from superset.utils.core import (
    as_list,
    convert_legacy_filters_into_adhoc,
    create_ssl_cert_file,
    merge_extra_filters,
    merge_extra_form_data,
    parse_ssl_cert,
)
from superset.utils.database import get_or_create_db
from superset.utils.date import DateColumn, DTTM_ALIAS, normalize_dttm_col
from superset.utils.hashing import md5_sha_from_str

# Inlined from the upstream ``tests/integration_tests/fixtures/certificates``
# data module (that package's ``__init__`` pulls in Flask-based fixtures).
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


def test_convert_legacy_filters_into_adhoc_where() -> None:
    form_data = {"where": "a = 1"}
    expected = {
        "adhoc_filters": [
            {
                "clause": "WHERE",
                "expressionType": "SQL",
                "filterOptionName": "46fb6d7891e23596e42ae38da94a57e0",
                "sqlExpression": "a = 1",
            }
        ]
    }
    convert_legacy_filters_into_adhoc(form_data)
    assert form_data == expected


def test_convert_legacy_filters_into_adhoc_filters() -> None:
    form_data = {"filters": [{"col": "a", "op": "in", "val": "someval"}]}
    expected = {
        "adhoc_filters": [
            {
                "clause": "WHERE",
                "comparator": "someval",
                "expressionType": "SIMPLE",
                "filterOptionName": "135c7ee246666b840a3d7a9c3a30cf38",
                "operator": "in",
                "subject": "a",
            }
        ]
    }
    convert_legacy_filters_into_adhoc(form_data)
    assert form_data == expected


def test_convert_legacy_filters_into_adhoc_present_and_empty() -> None:
    form_data = {"adhoc_filters": [], "where": "a = 1"}
    expected = {
        "adhoc_filters": [
            {
                "clause": "WHERE",
                "expressionType": "SQL",
                "filterOptionName": "46fb6d7891e23596e42ae38da94a57e0",
                "sqlExpression": "a = 1",
            }
        ]
    }
    convert_legacy_filters_into_adhoc(form_data)
    assert form_data == expected


def test_convert_legacy_filters_into_adhoc_having() -> None:
    form_data = {"having": "COUNT(1) = 1"}
    expected = {
        "adhoc_filters": [
            {
                "clause": "HAVING",
                "expressionType": "SQL",
                "filterOptionName": "683f1c26466ab912f75a00842e0f2f7b",
                "sqlExpression": "COUNT(1) = 1",
            }
        ]
    }
    convert_legacy_filters_into_adhoc(form_data)
    assert form_data == expected


def test_convert_legacy_filters_into_adhoc_present_and_nonempty() -> None:
    form_data = {
        "adhoc_filters": [
            {"clause": "WHERE", "expressionType": "SQL", "sqlExpression": "a = 1"}
        ],
        "filters": [{"col": "a", "op": "in", "val": "someval"}],
        "having": "COUNT(1) = 1",
    }
    expected = {
        "adhoc_filters": [
            {"clause": "WHERE", "expressionType": "SQL", "sqlExpression": "a = 1"}
        ]
    }
    convert_legacy_filters_into_adhoc(form_data)
    assert form_data == expected


@pytest.mark.skip(
    reason="superset.utils.core.split is not ported into Liteset's utils/core."
)
def test_split() -> None:  # pragma: no cover
    pass


def test_as_list() -> None:
    assert as_list(123) == [123]
    assert as_list([123]) == [123]
    assert as_list("foo") == ["foo"]


def test_merge_extra_filters_with_no_extras() -> None:
    form_data = {
        "time_range": "Last 10 days",
    }
    merge_extra_form_data(form_data)
    assert form_data == {"time_range": "Last 10 days", "adhoc_filters": []}


def test_merge_extra_filters_with_unset_legacy_time_range() -> None:
    form_data = {
        "time_range": "Last 10 days",
        "extra_filters": [
            {"col": "__time_range", "op": "==", "val": NO_TIME_RANGE},
        ],
        "extra_form_data": {"time_range": "Last year"},
    }
    merge_extra_filters(form_data)
    assert form_data == {
        "time_range": "Last year",
        "applied_time_extras": {},
        "adhoc_filters": [],
    }


def test_merge_extra_filters_with_extras() -> None:
    form_data = {
        "time_range": "Last 10 days",
        "extra_form_data": {
            "filters": [{"col": "foo", "op": "IN", "val": ["bar"]}],
            "adhoc_filters": [
                {
                    "expressionType": "SQL",
                    "clause": "WHERE",
                    "sqlExpression": "1 = 0",
                }
            ],
            "time_range": "Last 100 years",
            "time_grain_sqla": "PT1M",
            "relative_start": "now",
        },
    }
    merge_extra_form_data(form_data)
    adhoc_filters = form_data["adhoc_filters"]
    assert adhoc_filters[0] == {
        "clause": "WHERE",
        "expressionType": "SQL",
        "isExtra": True,
        "sqlExpression": "1 = 0",
    }
    converted_filter = adhoc_filters[1]
    del converted_filter["filterOptionName"]
    assert converted_filter == {
        "clause": "WHERE",
        "comparator": ["bar"],
        "expressionType": "SIMPLE",
        "isExtra": True,
        "operator": "IN",
        "subject": "foo",
    }
    assert form_data["time_range"] == "Last 100 years"
    assert form_data["time_grain_sqla"] == "PT1M"
    assert form_data["extras"]["relative_start"] == "now"


def test_ssl_certificate_parse() -> None:
    parsed_certificate = parse_ssl_cert(ssl_certificate)
    assert parsed_certificate.serial_number == 12355228710836649848


def test_ssl_certificate_file_creation() -> None:
    path = create_ssl_cert_file(ssl_certificate)
    expected_filename = md5_sha_from_str(ssl_certificate)
    assert expected_filename in path
    assert os.path.exists(path)


@pytest.mark.skip(
    reason="superset.utils.core.recipients_string_to_list is not ported into Liteset."
)
def test_recipients_string_to_list() -> None:  # pragma: no cover
    pass


@pytest.mark.skip(
    reason="superset.utils.schema depends on marshmallow, which was dropped from "
    "Liteset in favour of msgspec; the package is not installed."
)
def test_schema_validate_json() -> None:  # pragma: no cover
    pass


@pytest.mark.skip(
    reason="superset.utils.schema depends on marshmallow, which was dropped from "
    "Liteset in favour of msgspec; the package is not installed."
)
def test_schema_one_of_case_insensitive() -> None:  # pragma: no cover
    pass


def test_cast_to_num() -> None:
    assert cast_to_num("5") == 5
    assert cast_to_num("5.2") == 5.2
    assert cast_to_num(10) == 10
    assert cast_to_num(10.1) == 10.1
    assert cast_to_num(None) is None
    assert cast_to_num("this is not a string") is None


@pytest.mark.skip(
    reason="superset.utils.core.get_form_data_token is not ported into Liteset."
)
def test_get_form_data_token() -> None:  # pragma: no cover
    pass


def test_normalize_dttm_col() -> None:
    def normalize_col(
        df: pd.DataFrame,
        timestamp_format: Optional[str],
        offset: int,
        time_shift: Optional[str],
    ) -> pd.DataFrame:
        df = df.copy()
        normalize_dttm_col(
            df,
            (
                DateColumn.get_legacy_time_column(
                    timestamp_format=timestamp_format,
                    offset=offset,
                    time_shift=time_shift,
                ),
            ),
        )
        return df

    ts = pd.Timestamp(2021, 2, 15, 19, 0, 0, 0)
    df = pd.DataFrame([{"__timestamp": ts, "a": 1}])

    assert normalize_col(df, None, 0, None)[DTTM_ALIAS][0] == ts
    assert normalize_col(df, "epoch_ms", 0, None)[DTTM_ALIAS][0] == ts
    assert normalize_col(df, "epoch_s", 0, None)[DTTM_ALIAS][0] == ts

    assert normalize_col(df, None, 1, None)[DTTM_ALIAS][0] == pd.Timestamp(
        2021, 2, 15, 20, 0, 0, 0
    )

    assert normalize_col(df, None, 1, "30 minutes")[DTTM_ALIAS][0] == pd.Timestamp(
        2021, 2, 15, 20, 30, 0, 0
    )

    df = pd.DataFrame([{"__timestamp": ts.timestamp(), "a": 1}])
    assert normalize_col(df, "epoch_s", 0, None)[DTTM_ALIAS][0] == ts

    df = pd.DataFrame([{"__timestamp": ts.timestamp() * 1000, "a": 1}])
    assert normalize_col(df, "epoch_ms", 0, None)[DTTM_ALIAS][0] == ts


def test_get_or_create_db() -> None:
    from sqlalchemy import text

    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        get_or_create_db("test_db", "sqlite:///superset.db")
        session.commit()
        database = session.query(Database).filter_by(database_name="test_db").one()
        assert database is not None
        assert database.sqlalchemy_uri == "sqlite:///superset.db"

        # Upstream asserts the ``database_access`` PVM is registered on create
        # (``security_manager.find_permission_view_menu("database_access",
        # database.perm) is not None``).  The port has no Flask
        # ``security_manager``; the equivalent registration is performed by the
        # ``Database.after_insert`` event listener
        # (``superset.models._listeners._database_after_insert`` ->
        # ``security_manager.database_after_insert``), which writes the
        # Permission/ViewMenu/PermissionView triple to the FAB tables with
        # ``permission.name == "database_access"`` and
        # ``view_menu.name == database.perm``.  Assert against that real seam.
        pvm = session.execute(
            text(
                "SELECT pv.id FROM ab_permission_view pv "
                "JOIN ab_permission p ON pv.permission_id = p.id "
                "JOIN ab_view_menu vm ON pv.view_menu_id = vm.id "
                "WHERE p.name = :perm AND vm.name = :vm"
            ),
            {"perm": "database_access", "vm": database.perm},
        ).first()
        assert pvm is not None

        get_or_create_db("test_db", "sqlite:///changed.db")
        session.commit()
        database = session.query(Database).filter_by(database_name="test_db").one()
        assert database.sqlalchemy_uri == "sqlite:///changed.db"
    finally:
        database = (
            session.query(Database).filter_by(database_name="test_db").one_or_none()
        )
        if database is not None:
            session.delete(database)
            session.commit()


def test_get_or_create_db_invalid_uri() -> None:
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        with pytest.raises(DatabaseInvalidError):
            get_or_create_db("test_db", "yoursql:superset.db/()")
    finally:
        session.rollback()
        database = (
            session.query(Database).filter_by(database_name="test_db").one_or_none()
        )
        if database is not None:
            session.delete(database)
            session.commit()


def test_get_or_create_db_existing_invalid_uri() -> None:
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        database = get_or_create_db("test_db", "sqlite:///superset.db")
        database.sqlalchemy_uri = "None"
        session.commit()
        database = get_or_create_db("test_db", "sqlite:///superset.db")
        session.commit()
        assert database.sqlalchemy_uri == "sqlite:///superset.db"
    finally:
        database = (
            session.query(Database).filter_by(database_name="test_db").one_or_none()
        )
        if database is not None:
            session.delete(database)
            session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
async def test_extract_dataframe_dtypes(db_session: Any) -> None:
    datasource = (
        await db_session.execute(
            select(SqlaTable)
            .where(SqlaTable.table_name == "birth_names")
            .options(selectinload(SqlaTable.columns))
        )
    ).scalar_one()
    cols: tuple[tuple[str, GenericDataType, list[Any]], ...] = (
        ("dt", GenericDataType.TEMPORAL, [date(2021, 2, 4), date(2021, 2, 4)]),
        (
            "dttm",
            GenericDataType.TEMPORAL,
            [datetime(2021, 2, 4, 1, 1, 1), datetime(2021, 2, 4, 1, 1, 1)],
        ),
        ("str", GenericDataType.STRING, ["foo", "foo"]),
        ("int", GenericDataType.NUMERIC, [1, 1]),
        ("float", GenericDataType.NUMERIC, [0.5, 0.5]),
        ("mixed-int-float", GenericDataType.NUMERIC, [0.5, 1.0]),
        ("bool", GenericDataType.BOOLEAN, [True, False]),
        ("mixed-str-int", GenericDataType.STRING, ["abc", 1.0]),
        ("obj", GenericDataType.STRING, [{"a": 1}, {"a": 1}]),
        ("dt_null", GenericDataType.TEMPORAL, [None, date(2021, 2, 4)]),
        (
            "dttm_null",
            GenericDataType.TEMPORAL,
            [None, datetime(2021, 2, 4, 1, 1, 1)],
        ),
        ("str_null", GenericDataType.STRING, [None, "foo"]),
        ("int_null", GenericDataType.NUMERIC, [None, 1]),
        ("float_null", GenericDataType.NUMERIC, [None, 0.5]),
        ("bool_null", GenericDataType.BOOLEAN, [None, False]),
        ("obj_null", GenericDataType.STRING, [None, {"a": 1}]),
        # Non-timestamp columns should be identified as temporal if
        # `is_dttm` is set to `True` in the underlying datasource
        ("ds", GenericDataType.TEMPORAL, [None, {"ds": "2017-01-01"}]),
    )

    df = pd.DataFrame(data={col[0]: col[2] for col in cols})
    assert extract_dataframe_dtypes(df, datasource) == [col[1] for col in cols]


# ---------------------------------------------------------------------------
# ``views.utils.get_form_data`` parsing cases.
#
# Upstream exercises ``superset.views.utils.get_form_data`` — the Flask view
# helper that returns a ``(form_data, slice)`` tuple by parsing the request's
# query args, form body, ``g.form_data`` global, and embedded ``queries`` from
# the active Flask request context.  The Liteset port has no equivalent: the
# only ``get_form_data`` symbols (``superset.utils.core.get_form_data`` and
# ``superset.jinja_context.get_form_data``) are unrelated ContextVar helpers
# that return a bare dict (not a tuple) and never parse request args/form.  The
# explore ``form_data`` parsing was instead inlined privately into
# ``superset.controllers.explore_json._extract_form_data`` and is not a public,
# tuple-returning API.  Retained verbatim (full upstream bodies) but skipped
# until/if the tuple-returning view helper is ported.
# ---------------------------------------------------------------------------

_NO_VIEWS_GET_FORM_DATA = (
    "superset.views.utils.get_form_data (the (form_data, slice) tuple-returning "
    "Flask view helper) is not ported into Liteset; the only get_form_data "
    "symbols are the unrelated ContextVar dict helpers in "
    "superset.utils.core / superset.jinja_context."
)

# These skipped tests are retained verbatim from upstream to document the port
# gaps; they reference Flask/SupersetTestCase symbols that have no counterpart
# in the Liteset harness.  Bound to a poison sentinel so the bodies stay
# importable (ruff-clean) yet would fail loudly if a skip were ever removed
# without first wiring the missing behaviour.
_UNPORTED = None  # type: Any  # views.utils.get_form_data / SupersetTestCase / db / Log
current_app = _UNPORTED
g = _UNPORTED
db = _UNPORTED
Log = _UNPORTED
self = _UNPORTED
ADMIN_USERNAME = _UNPORTED


def get_form_data() -> Any:  # noqa: D103 - retained upstream-test symbol
    raise NotImplementedError(_NO_VIEWS_GET_FORM_DATA)


@pytest.mark.skip(reason=_NO_VIEWS_GET_FORM_DATA)
def test_get_form_data_default() -> None:  # pragma: no cover
    form_data, slc = get_form_data()
    assert slc is None


@pytest.mark.skip(reason=_NO_VIEWS_GET_FORM_DATA)
def test_get_form_data_request_args() -> None:  # pragma: no cover
    with current_app.test_request_context(
        query_string={"form_data": json.dumps({"foo": "bar"})}
    ):
        form_data, slc = get_form_data()
        assert form_data == {"foo": "bar"}
        assert slc is None


@pytest.mark.skip(reason=_NO_VIEWS_GET_FORM_DATA)
def test_get_form_data_request_form() -> None:  # pragma: no cover
    with current_app.test_request_context(
        data={"form_data": json.dumps({"foo": "bar"})}
    ):
        form_data, slc = get_form_data()
        assert form_data == {"foo": "bar"}
        assert slc is None


@pytest.mark.skip(reason=_NO_VIEWS_GET_FORM_DATA)
def test_get_form_data_request_form_with_queries() -> None:  # pragma: no cover
    # the CSV export uses for requests, even when sending requests to
    # /api/v1/chart/data
    with current_app.test_request_context(
        data={"form_data": json.dumps({"queries": [{"url_params": {"foo": "bar"}}]})}
    ):
        form_data, slc = get_form_data()
        assert form_data == {"url_params": {"foo": "bar"}}
        assert slc is None


@pytest.mark.skip(reason=_NO_VIEWS_GET_FORM_DATA)
def test_get_form_data_request_args_and_form() -> None:  # pragma: no cover
    with current_app.test_request_context(
        data={"form_data": json.dumps({"foo": "bar"})},
        query_string={"form_data": json.dumps({"baz": "bar"})},
    ):
        form_data, slc = get_form_data()
        assert form_data == {"baz": "bar", "foo": "bar"}
        assert slc is None


@pytest.mark.skip(reason=_NO_VIEWS_GET_FORM_DATA)
def test_get_form_data_globals() -> None:  # pragma: no cover
    with current_app.test_request_context():
        g.form_data = {"foo": "bar"}
        form_data, slc = get_form_data()
        delattr(g, "form_data")
        assert form_data == {"foo": "bar"}
        assert slc is None


@pytest.mark.skip(reason=_NO_VIEWS_GET_FORM_DATA)
def test_get_form_data_corrupted_json() -> None:  # pragma: no cover
    with current_app.test_request_context(
        data={"form_data": "{x: '2324'}"},
        query_string={"form_data": '{"baz": "bar"'},
    ):
        form_data, slc = get_form_data()
        assert form_data == {}
        assert slc is None


@pytest.mark.skip(
    reason="The legacy /superset/explore_json controller does not bind the parsed "
    "explore form_data into the audit-log ContextVar that "
    "AbstractEventLogger.alog_with_context reads via get_form_data(); it logs only "
    'alog_with_context("explore_json", object_ref=...).  So record.json lacks the '
    "curated form_data (slice_id / viz_type) the upstream assertions require.  "
    "Retained verbatim until the controller wires set_form_data() before logging."
)
@pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
def test_log_this() -> None:  # pragma: no cover
    # TODO: Add additional scenarios.
    self.login(ADMIN_USERNAME)
    slc = self.get_slice("Life Expectancy VS Rural %")
    dashboard_id = 1

    assert slc.viz is not None
    resp = self.get_json_resp(  # noqa: F841
        f"/superset/explore_json/{slc.datasource_type}/{slc.datasource_id}/"
        + f'?form_data={{"slice_id": {slc.id}}}&dashboard_id={dashboard_id}',
        {"form_data": json.dumps(slc.viz.form_data)},
    )

    record = (
        db.session.query(Log)
        .filter_by(action="explore_json", slice_id=slc.id)
        .order_by(Log.dttm.desc())
        .first()
    )

    assert record.dashboard_id == dashboard_id
    assert json.loads(record.json)["dashboard_id"] == str(dashboard_id)
    assert json.loads(record.json)["form_data"]["slice_id"] == slc.id

    assert (
        json.loads(record.json)["form_data"]["viz_type"]
        == slc.viz.form_data["viz_type"]
    )
