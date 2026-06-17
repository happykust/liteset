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
"""Flask-free port of the cache-warmup strategy integration tests.

The strategy classes (:class:`TopNDashboardsStrategy`,
:class:`DashboardTagsStrategy`) run **synchronously** against the
:func:`superset.db.session.get_sync_session` thread-local Session (the same
engine the Celery worker uses).  These tests build the required rows (visit
``Log`` entries, custom ``Tag`` / ``TaggedObject`` rows, and a small unicode
dataset + chart + dashboard) through that sync session, run the real strategy,
and assert on the produced task list.

The harness ``db_session`` (async, rolled back) is NOT used here because the
strategy opens its own sync session and would not see uncommitted async writes;
the sync session is committed and cleaned up explicitly instead.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine as _create_sync_engine, String

from superset.db.session import get_sync_session
from superset.models.connectors import SqlaTable
from superset.models.core import Database, Log
from superset.models.dashboard import Dashboard
from superset.models.slice import Slice
from superset.models.tags import ObjectType, Tag, TaggedObject, TagType
from superset.tasks.cache import DashboardTagsStrategy, TopNDashboardsStrategy

UNICODE_TBL_NAME = "unicode_test"

_UNICODE_DATA = [
    {"phrase": "Под"},
    {"phrase": "řšž"},
    {"phrase": "視野無限廣"},
    {"phrase": "微風"},
    {"phrase": "中国智造"},
    {"phrase": "æøå"},
    {"phrase": "ëœéè"},
    {"phrase": "いろはにほ"},
]


def _sync_uri() -> str:
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    uri = settings.sqlalchemy_examples_uri or settings.sqlalchemy_database_uri
    return uri.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)


def _load_unicode_physical_table() -> None:
    """Port of ``load_unicode_data``: write the ``unicode_test`` SQL table."""
    engine = _create_sync_engine(_sync_uri())
    try:
        pd.DataFrame.from_dict(_UNICODE_DATA).to_sql(
            UNICODE_TBL_NAME,
            engine,
            if_exists="replace",
            chunksize=500,
            dtype={"phrase": String(500)},
            index=False,
            method="multi",
        )
    finally:
        engine.dispose()


def _get_dash_by_slug(session, slug: str) -> Dashboard:
    return session.query(Dashboard).filter_by(slug=slug).one()


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
async def test_top_n_dashboards_strategy(db_session) -> None:
    session = get_sync_session()
    try:
        session.query(Log).delete()
        session.commit()

        dash = _get_dash_by_slug(session, "births")
        for _ in range(10):
            session.add(
                Log(dashboard_id=dash.id, action="dashboard", dttm=datetime.utcnow())
            )
        session.commit()

        strategy = TopNDashboardsStrategy(1)
        result = strategy.get_tasks()
        expected = [
            {
                "payload": {"chart_id": chart.id, "dashboard_id": dash.id},
            }
            for chart in dash.slices
        ]
        assert len(result) == len(expected)
    finally:
        cleanup = get_sync_session()
        cleanup.query(Log).delete()
        cleanup.commit()
        cleanup.close()


def _reset_tag(session, tag: Tag) -> None:
    """Remove associated objects from a tag, to make the test idempotent."""
    if tag.objects:
        for o in tag.objects:
            session.delete(o)
        session.commit()


def _get_or_create_tag(session, name: str) -> Tag:
    tag = session.query(Tag).filter_by(name=name).one_or_none()
    if tag is None:
        tag = Tag(name=name, type=TagType.custom)
        session.add(tag)
        session.commit()
    return tag


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
async def test_dashboard_tags_strategy(db_session) -> None:
    _load_unicode_physical_table()
    session = get_sync_session()

    # Build the unicode dataset + chart + dashboard (port of
    # load_unicode_dashboard_with_slice). The strategy only needs the Slice and
    # Dashboard ORM rows; the physical table backs the dataset faithfully.
    created: dict[str, object] = {}
    try:
        example_db = session.query(Database).filter_by(database_name="examples").one()

        table = SqlaTable(table_name=UNICODE_TBL_NAME, database_id=example_db.id)
        session.add(table)
        session.flush()

        unicode_slice = Slice(
            slice_name="Unicode Cloud",
            viz_type="word_cloud",
            datasource_id=table.id,
            datasource_type="table",
            params="{}",
        )
        session.add(unicode_slice)
        session.flush()

        unicode_dash = Dashboard(
            slug="unicode-test",
            dashboard_title="Unicode Test",
            position_json=None,
        )
        unicode_dash.slices = [unicode_slice]
        session.add(unicode_dash)
        session.commit()
        created.update(table=table, slice=unicode_slice, dashboard=unicode_dash)

        tag1 = _get_or_create_tag(session, "tag1")
        # delete first to make test idempotent
        _reset_tag(session, tag1)

        strategy = DashboardTagsStrategy(["tag1"])
        assert strategy.get_tasks() == []

        tag1 = _get_or_create_tag(session, "tag1")
        births = _get_dash_by_slug(session, "births")
        tag1_payloads = [{"chart_id": chart.id} for chart in births.slices]
        session.add(
            TaggedObject(
                tag_id=tag1.id,
                object_id=births.id,
                object_type=ObjectType.dashboard,
            )
        )
        session.commit()

        assert len(strategy.get_tasks()) == len(tag1_payloads)

        strategy = DashboardTagsStrategy(["tag2"])
        tag2 = _get_or_create_tag(session, "tag2")
        _reset_tag(session, tag2)

        assert strategy.get_tasks() == []

        dash = _get_dash_by_slug(session, "unicode-test")
        chart = dash.slices[0]
        tag2_payloads = [{"chart_id": chart.id}]
        session.add(
            TaggedObject(
                tag_id=tag2.id,
                object_id=chart.id,
                object_type=ObjectType.chart,
            )
        )
        session.commit()

        assert len(strategy.get_tasks()) == len(tag2_payloads)

        strategy = DashboardTagsStrategy(["tag1", "tag2"])

        assert len(strategy.get_tasks()) == len(tag1_payloads + tag2_payloads)
    finally:
        cleanup = get_sync_session()
        for tag_name in ("tag1", "tag2"):
            tag = cleanup.query(Tag).filter_by(name=tag_name).one_or_none()
            if tag:
                for o in list(tag.objects):
                    cleanup.delete(o)
                cleanup.delete(tag)
        cleanup.commit()
        dash = cleanup.query(Dashboard).filter_by(slug="unicode-test").one_or_none()
        if dash:
            cleanup.delete(dash)
        sl = cleanup.query(Slice).filter_by(slice_name="Unicode Cloud").one_or_none()
        if sl:
            cleanup.delete(sl)
        tbl = (
            cleanup.query(SqlaTable)
            .filter_by(table_name=UNICODE_TBL_NAME)
            .one_or_none()
        )
        if tbl:
            cleanup.delete(tbl)
        cleanup.commit()
        cleanup.close()
        engine = _create_sync_engine(_sync_uri())
        try:
            with engine.begin() as conn:
                from sqlalchemy import text

                conn.execute(text(f"DROP TABLE IF EXISTS {UNICODE_TBL_NAME}"))
        finally:
            engine.dispose()
