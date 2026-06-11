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
"""Round-10 controller/listener regressions.

* dataset rename: ``_sqlatable_before_update`` must rename the
  ``ab_view_menu`` entry and propagate ``perm`` to the dataset's charts
  (1:1 ``security_manager.dataset_before_update``) — previously only
  ``target.perm`` was refreshed and explicit grants broke.
* dashboard v0 import: ``filter_scopes``-only params raised NameError
  (``converted_scopes`` unbound).
* GET /chart/{pk}/data GAQ path: must enforce ``raise_for_access`` before
  dispatching the Celery job (the POST path already did).
* dashboard /datasets: ``granularity_sqla`` must be ``[[value, label]]``
  pairs (``choicify``), not a flat list of names.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from superset.exceptions import ObjectNotFoundError  # noqa: F401


@pytest.fixture
def sync_env():
    """Sync sqlite session with all models + listeners registered."""
    import superset.models  # noqa: F401  (registers models AND listeners)
    from superset.models.helpers import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        yield session
    engine.dispose()


# ---------------------------------------------------------------------------
# Dataset rename listener (high)
# ---------------------------------------------------------------------------


def test_dataset_rename_updates_view_menu_and_chart_perms(sync_env):
    from superset.models.connectors import SqlaTable
    from superset.models.core import Database
    from superset.models.slice import Slice

    session = sync_env
    db = Database(database_name="examples", sqlalchemy_uri="sqlite://")
    session.add(db)
    session.flush()

    dataset = SqlaTable(table_name="old_name", database_id=db.id)
    session.add(dataset)
    session.flush()  # after_insert creates the datasource_access PVM
    session.flush()  # persist the perm assigned in after_insert
    old_perm = dataset.perm
    assert old_perm == f"[examples].[old_name](id:{dataset.id})"

    chart = Slice(
        slice_name="c1",
        datasource_id=dataset.id,
        datasource_type="table",
        viz_type="table",
        params="{}",
    )
    session.add(chart)
    session.flush()
    assert chart.perm == old_perm

    # Rename the dataset
    dataset.table_name = "new_name"
    session.flush()
    session.commit()

    new_perm = f"[examples].[new_name](id:{dataset.id})"

    # ab_view_menu renamed (grants on the old PVM survive the rename)
    vm_names = {
        row.name
        for row in session.execute(text("SELECT name FROM ab_view_menu")).fetchall()
    }
    assert new_perm in vm_names
    assert old_perm not in vm_names

    # dataset + chart perm fields updated
    row = session.execute(
        text("SELECT perm FROM tables WHERE id = :id"), {"id": dataset.id}
    ).first()
    assert row.perm == new_perm
    row = session.execute(
        text("SELECT perm FROM slices WHERE id = :id"), {"id": chart.id}
    ).first()
    assert row.perm == new_perm


def test_dataset_schema_change_updates_schema_perms(sync_env):
    from superset.models.connectors import SqlaTable
    from superset.models.core import Database
    from superset.models.slice import Slice

    session = sync_env
    db = Database(database_name="examples", sqlalchemy_uri="sqlite://")
    session.add(db)
    session.flush()

    dataset = SqlaTable(table_name="t", database_id=db.id, schema="old_schema")
    session.add(dataset)
    session.flush()
    chart = Slice(
        slice_name="c1",
        datasource_id=dataset.id,
        datasource_type="table",
        viz_type="table",
        params="{}",
    )
    session.add(chart)
    session.flush()

    dataset.schema = "new_schema"
    session.flush()
    session.commit()

    expected = "[examples].[new_schema]"
    row = session.execute(
        text("SELECT schema_perm FROM tables WHERE id = :id"), {"id": dataset.id}
    ).first()
    assert row.schema_perm == expected
    row = session.execute(
        text("SELECT schema_perm FROM slices WHERE id = :id"), {"id": chart.id}
    ).first()
    assert row.schema_perm == expected
    # schema_access PVM created
    vm = session.execute(
        text("SELECT id FROM ab_view_menu WHERE name = :n"), {"n": expected}
    ).first()
    assert vm is not None


# ---------------------------------------------------------------------------
# dashboard v0 import — filter_scopes-only params (high)
# ---------------------------------------------------------------------------


def test_dashboard_v0_import_filter_scopes_only_no_nameerror(sync_env):
    from superset.importexport.legacy.dashboard_v0 import import_dashboard
    from superset.models.dashboard import Dashboard

    session = sync_env
    scopes = {"101": {"region": {"scope": ["ROOT_ID"], "immune": []}}}
    dash = Dashboard(
        dashboard_title="v0 dash",
        params=json.dumps({"remote_id": 9999, "filter_scopes": scopes}),
        json_metadata=json.dumps({"filter_scopes": scopes}),
        position_json="{}",
        slices=[],
    )

    # Pre-fix: NameError (converted_scopes referenced before assignment).
    new_id = import_dashboard(session, dash, import_time=1700000000)
    assert isinstance(new_id, int)


# ---------------------------------------------------------------------------
# GET /chart/{pk}/data GAQ — raise_for_access before dispatch (high)
# ---------------------------------------------------------------------------


def _get_raw_method(controller_cls: type, method_name: str):
    handler = getattr(controller_cls, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


async def test_get_chart_data_gaq_enforces_datasource_access():
    from superset.controllers.chart import ChartController
    from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
    from superset.exceptions import SupersetSecurityException

    controller = ChartController(owner=MagicMock())
    chart = MagicMock()
    chart.query_context = json.dumps(
        {
            "datasource": {"type": "table", "id": 1},
            "queries": [{"columns": ["col1"]}],
            "force": False,
        }
    )
    dao = AsyncMock()
    dao.find_all = AsyncMock(return_value=[chart])
    ds_dao = AsyncMock()
    ds_dao.get_datasource = AsyncMock(return_value=MagicMock())

    sm = MagicMock()
    sm.raise_for_access = AsyncMock(
        side_effect=SupersetSecurityException(
            SupersetError(
                error_type=SupersetErrorType.DATASOURCE_SECURITY_ACCESS_ERROR,
                message="denied",
                level=ErrorLevel.ERROR,
            )
        )
    )

    state = MagicMock()
    settings = MagicMock()
    settings.global_async_queries = True
    settings.feature_flags = {}
    state.settings = settings

    user = MagicMock()
    user.id = 1

    get_chart_data = _get_raw_method(ChartController, "get_chart_data")
    with pytest.raises(SupersetSecurityException):
        await get_chart_data(
            controller,
            request=MagicMock(),
            pk=1,
            dao=dao,
            ds_dao=ds_dao,
            security_manager=sm,
            current_user=user,
            state=state,
        )
    sm.raise_for_access.assert_awaited_once()


# ---------------------------------------------------------------------------
# dashboard /datasets — granularity_sqla pairs (high)
# ---------------------------------------------------------------------------


async def test_get_datasets_granularity_sqla_is_value_label_pairs():
    from superset.controllers.dashboard import DashboardController

    controller = DashboardController(owner=MagicMock())
    column = SimpleNamespace(
        column_name="created_at",
        verbose_name=None,
        is_dttm=True,
        type="TIMESTAMP",
        groupby=True,
        filterable=True,
        expression=None,
    )
    dataset = SimpleNamespace(
        id=7,
        table_name="t",
        main_dttm_col="created_at",
        columns=[column],
        metrics=[],
        owners=[],
        database=None,
        schema=None,
        catalog=None,
        filter_select_enabled=False,
        sql=None,
        offset=0,
        cache_timeout=None,
        params=None,
        perm=None,
        normalize_columns=False,
        always_filter_main_dttm=False,
        is_sqllab_view=False,
        template_params=None,
        fetch_values_predicate=None,
        default_endpoint=None,
        verbose_map={},
        column_formats={},
    )
    dashboard = MagicMock()
    dao = MagicMock()
    dao.get_full_by_id_or_slug = AsyncMock(return_value=dashboard)
    dao.get_datasets_for_dashboard = AsyncMock(return_value=[dataset])
    sm = MagicMock()
    sm.raise_for_access = AsyncMock()
    sm.is_guest_user = MagicMock(return_value=False)

    get_datasets = _get_raw_method(DashboardController, "get_datasets")
    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[]),
    ):
        result = await get_datasets(
            controller,
            id_or_slug="1",
            dao=dao,
            security_manager=sm,
            current_user=MagicMock(),
        )

    ds_payload = result["result"][0]
    assert ds_payload["granularity_sqla"] == [["created_at", "created_at"]]
