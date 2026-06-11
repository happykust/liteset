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
"""Round-11 regressions (manual full-pass findings).

* R11-15 — ``is_timeseries`` auto-detection (``__timestamp in columns``) was
  dead: the msgspec schema defaulted to ``False`` and both ``from_request``
  paths substituted ``False`` for an absent value, so the round-6
  ``bool | None`` dataclass semantics never received ``None``.
* R11-02 — the shared ``_import_dataset`` (chart/dashboard/database/assets
  bundles) dropped upstream's ``not table_exists`` branch of the data-URI
  load (only honouring the never-passed ``force_data``).
* R11-01 — ``_load_data``'s allow-list validation imported the phantom
  ``superset.config.current_config`` (silently skipping the check) and
  treated an EMPTY allow-list as allow-all (upstream: deny-all).
* R11-05 — chart-importer wrapped ``import_tag`` in a swallow-all
  ``try/except`` (upstream lets failures roll back the whole import).
* R11-11/R11-12 — the dataset importer dropped ``is_managed_externally`` /
  ``external_url`` / ``folders`` (upstream imports them via
  ``extra_import_fields`` / ``export_fields``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import msgspec
import pytest

from superset.common.query_object import AsyncQueryObject

DS_REF = {"type": "table", "id": 1}


@pytest.fixture
async def async_session():
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import superset.models  # noqa: F401  (register models)
    from superset.models.helpers import Base

    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# R11-15 — is_timeseries auto-detection
# ---------------------------------------------------------------------------


def test_from_request_dict_autodetects_is_timeseries_from_dttm_alias():
    qo = AsyncQueryObject.from_request(
        {"columns": ["__timestamp", "gender"], "metrics": ["count"]},
        DS_REF,
    )
    assert qo.is_timeseries is True


def test_from_request_dict_no_dttm_alias_is_not_timeseries():
    qo = AsyncQueryObject.from_request(
        {"columns": ["gender"], "metrics": ["count"]},
        DS_REF,
    )
    assert qo.is_timeseries is False


def test_from_request_dict_explicit_false_stays_false():
    qo = AsyncQueryObject.from_request(
        {
            "columns": ["__timestamp"],
            "metrics": ["count"],
            "is_timeseries": False,
        },
        DS_REF,
    )
    assert qo.is_timeseries is False


def test_from_request_struct_autodetects_is_timeseries():
    from superset.schemas.chart import ChartDataQueryObject

    q = msgspec.convert(
        {"columns": ["__timestamp"], "metrics": ["count"]},
        type=ChartDataQueryObject,
    )
    # Schema default must be None (absent), not an explicit False.
    assert q.is_timeseries is None
    qo = AsyncQueryObject.from_request(q, DS_REF)
    assert qo.is_timeseries is True


def test_from_request_struct_explicit_false_stays_false():
    from superset.schemas.chart import ChartDataQueryObject

    q = msgspec.convert(
        {"columns": ["__timestamp"], "is_timeseries": False},
        type=ChartDataQueryObject,
    )
    qo = AsyncQueryObject.from_request(q, DS_REF)
    assert qo.is_timeseries is False


# ---------------------------------------------------------------------------
# R11-01 — _load_data allow-list semantics (deny-all on empty list)
# ---------------------------------------------------------------------------


class _Settings:
    def __init__(self, urls: list[str]) -> None:
        self.dataset_import_allowed_data_urls = urls


@pytest.mark.asyncio
async def test_shared_load_data_rejects_uri_not_in_allow_list():
    from superset.commands.chart.importers.v1 import utils as chart_utils
    from superset.commands.dataset.exceptions import DatasetForbiddenDataURI

    with patch.object(
        chart_utils, "_dataset_import_allowed_urls", return_value=["https://ok/.*"]
    ):
        with pytest.raises(DatasetForbiddenDataURI):
            await chart_utils._load_data(None, "https://evil/x.csv", object())


@pytest.mark.asyncio
async def test_shared_load_data_empty_allow_list_denies_all():
    from superset.commands.chart.importers.v1 import utils as chart_utils
    from superset.commands.dataset.exceptions import DatasetForbiddenDataURI

    with patch.object(chart_utils, "_dataset_import_allowed_urls", return_value=[]):
        with pytest.raises(DatasetForbiddenDataURI):
            await chart_utils._load_data(None, "https://anything/x.csv", object())


# ---------------------------------------------------------------------------
# R11-02 — _import_dataset loads data when the physical table is missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_import_dataset_loads_data_when_table_missing(
    async_session: Any,
) -> None:
    from superset.commands.chart.importers.v1 import utils as chart_utils
    from superset.models.core import Database

    db = Database(database_name="r11_db", sqlalchemy_uri="sqlite://")
    async_session.add(db)
    await async_session.flush()

    config = {
        "uuid": "11111111-2222-3333-4444-555555550002",
        "table_name": "r11_tbl",
        "database_id": db.id,
        "data": "https://example.com/data.csv",
    }

    with (
        patch.object(chart_utils, "_table_exists", return_value=False) as mock_exists,
        patch.object(chart_utils, "_load_data") as mock_load,
    ):
        await chart_utils._import_dataset(async_session, dict(config))

    mock_exists.assert_awaited()
    mock_load.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_import_dataset_skips_data_when_table_exists(
    async_session: Any,
) -> None:
    from superset.commands.chart.importers.v1 import utils as chart_utils
    from superset.models.core import Database

    db = Database(database_name="r11_db2", sqlalchemy_uri="sqlite://")
    async_session.add(db)
    await async_session.flush()

    config = {
        "uuid": "11111111-2222-3333-4444-555555550003",
        "table_name": "r11_tbl2",
        "database_id": db.id,
        "data": "https://example.com/data.csv",
    }

    with (
        patch.object(chart_utils, "_table_exists", return_value=True),
        patch.object(chart_utils, "_load_data") as mock_load,
    ):
        await chart_utils._import_dataset(async_session, dict(config))

    mock_load.assert_not_awaited()


# ---------------------------------------------------------------------------
# R11-11 / R11-12 — dataset importer keeps external-management + folders
# ---------------------------------------------------------------------------


def test_dataset_importer_attrs_include_external_and_folders():
    import inspect as _inspect

    from superset.commands.dataset.importers.v1 import ImportDatasetsCommand

    src = _inspect.getsource(ImportDatasetsCommand._import_dataset)
    for attr in ("is_managed_externally", "external_url", "folders"):
        assert f'"{attr}"' in src, attr


# ---------------------------------------------------------------------------
# R11-16 — GET /{pk}/data/?format=csv returns a raw CSV file, not JSON
# ---------------------------------------------------------------------------


def _raw_handler(method_name: str):
    from superset.controllers.chart import ChartController

    handler = getattr(ChartController, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


def _csv_get_mocks():
    import json as _json
    from unittest.mock import AsyncMock, MagicMock

    chart = MagicMock()
    chart.query_context = _json.dumps(
        {
            "datasource": {"type": "table", "id": 1},
            "queries": [{"columns": ["col1"]}],
            "force": False,
        }
    )
    chart.params = None
    dao = AsyncMock()
    dao.find_all = AsyncMock(return_value=[chart])
    ds_dao = AsyncMock()
    ds_dao.get_datasource = AsyncMock(return_value=MagicMock())
    sm = MagicMock()
    sm.raise_for_access = AsyncMock()
    sm.is_guest_user = MagicMock(return_value=False)
    user = MagicMock()
    user.id = 1
    user.is_authenticated = True
    state = MagicMock()
    state.settings = MagicMock(global_async_queries=False, feature_flags={})
    return chart, dao, ds_dao, sm, user, state


@pytest.mark.asyncio
async def test_get_chart_data_csv_returns_raw_csv_file():
    from unittest.mock import AsyncMock, MagicMock

    from superset.controllers.chart import ChartController

    _get_chart_data = _raw_handler("get_chart_data")
    _chart, dao, ds_dao, sm, user, state = _csv_get_mocks()
    sm.can_access = AsyncMock(return_value=True)

    mock_cmd = AsyncMock()
    mock_cmd.execute = AsyncMock(
        return_value={"queries": [{"data": [{"col1": 1}, {"col1": 2}]}]}
    )
    with patch("superset.controllers.chart.ChartDataCommand", return_value=mock_cmd):
        resp = await _get_chart_data(
            ChartController(owner=MagicMock()),
            request=MagicMock(),
            pk=1,
            dao=dao,
            ds_dao=ds_dao,
            security_manager=sm,
            current_user=user,
            state=state,
            format="csv",
        )

    assert resp.media_type == "text/csv"
    body = resp.content
    text = body.decode() if isinstance(body, bytes) else str(body)
    assert "col1" in text
    assert not text.lstrip().startswith("{")
    assert "attachment" in resp.headers["Content-Disposition"]


@pytest.mark.asyncio
async def test_get_chart_data_csv_requires_can_csv():
    from unittest.mock import AsyncMock, MagicMock

    from superset.controllers.chart import ChartController

    _get_chart_data = _raw_handler("get_chart_data")
    _chart, dao, ds_dao, sm, user, state = _csv_get_mocks()
    sm.can_access = AsyncMock(return_value=False)

    resp = await _get_chart_data(
        ChartController(owner=MagicMock()),
        request=MagicMock(),
        pk=1,
        dao=dao,
        ds_dao=ds_dao,
        security_manager=sm,
        current_user=user,
        state=state,
        format="csv",
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# R11-17 — /database/available/ only lists engines with an installed driver
# ---------------------------------------------------------------------------


def test_get_installed_drivers_reflects_environment():
    from superset.db_engine_specs import get_installed_drivers

    drivers = get_installed_drivers()
    # sqlite + postgresql ship with the test env; an obviously-absent engine
    # must NOT appear.
    assert "sqlite" in drivers
    assert "postgresql" in drivers
    assert "this_engine_does_not_exist" not in drivers
