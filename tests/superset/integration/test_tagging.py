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
"""Flask-free port of ``tests/integration_tests/tagging_tests.py``.

Exercises the implicit-tag SQLAlchemy event listeners
(``superset.models._listeners``) which create / delete ``owner:`` /
``type:`` / ``favorited_by:`` ``TaggedObject`` rows when a Chart / Dashboard /
Dataset / SavedQuery / FavStar is committed.

Behaviour-preserving adaptations:

* The tag listeners fire on the **synchronous** ORM ``Session`` (they open a
  nested ``Session(bind=connection)`` inside ``after_insert`` /
  ``after_delete``), so the test commits objects on the sync Celery session
  (``get_sync_session``) rather than the async ``db_session``.
* The ``TAGGING_SYSTEM`` feature flag (off by default in the test env) is
  enabled and the tag listeners are registered for the duration of the test
  via the ``tagging_system`` fixture, mirroring the upstream
  ``with_tagging_system_feature`` fixture (which calls
  ``register_sqla_event_listeners`` / ``clear_sqla_event_listeners``).  The
  listeners are removed at teardown so the global mapper events do not leak
  into other tests.
* ``FavStar.user_id`` carries a real FK to ``ab_user``, which is empty in the
  seeded fixture DB, so a real ``User`` is created for the favorite test
  (upstream relied on the seeded admin id=1).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from superset.db.session import get_sync_session
from superset.models import _listeners
from superset.models.connectors import SqlaTable
from superset.models.core import FavStar
from superset.models.dashboard import Dashboard
from superset.models.security import User
from superset.models.slice import Slice
from superset.models.sql_lab import SavedQuery
from superset.models.tags import TaggedObject
from superset.utils.core import DatasourceType
from superset.utils.database import get_main_database
from superset.utils.feature_flags import feature_flag_manager

# (model, event_name, handler) triples wired by ``_register_tag_listeners``;
# kept here so the fixture can remove exactly what it registered.
_TAG_LISTENERS = [
    (Slice, "after_insert", _listeners._chart_tag_after_insert),
    (Slice, "after_update", _listeners._chart_tag_after_update),
    (Slice, "after_delete", _listeners._chart_tag_after_delete),
    (Dashboard, "after_insert", _listeners._dashboard_tag_after_insert),
    (Dashboard, "after_update", _listeners._dashboard_tag_after_update),
    (Dashboard, "after_delete", _listeners._dashboard_tag_after_delete),
    (SqlaTable, "after_insert", _listeners._dataset_tag_after_insert),
    (SqlaTable, "after_update", _listeners._dataset_tag_after_update),
    (SqlaTable, "after_delete", _listeners._dataset_tag_after_delete),
    (SavedQuery, "after_insert", _listeners._query_tag_after_insert),
    (SavedQuery, "after_update", _listeners._query_tag_after_update),
    (SavedQuery, "after_delete", _listeners._query_tag_after_delete),
    (FavStar, "after_insert", _listeners._favstar_after_insert),
    (FavStar, "after_delete", _listeners._favstar_after_delete),
]


@pytest.fixture
def sync_session() -> Iterator[Session]:
    session = get_sync_session()
    try:
        yield session
    finally:
        session.rollback()


@pytest.fixture
def with_tagging_system_feature() -> Iterator[None]:
    """Enable ``TAGGING_SYSTEM`` and register the implicit-tag listeners.

    Mirrors upstream ``with_tagging_system_feature``; registers the listeners
    only if not already wired and removes them at teardown.
    """
    saved = dict(feature_flag_manager._feature_flags)
    feature_flag_manager._feature_flags["TAGGING_SYSTEM"] = True
    registered: list[tuple[object, str, object]] = []
    for model, name, handler in _TAG_LISTENERS:
        if not event.contains(model, name, handler):
            event.listen(model, name, handler)
            registered.append((model, name, handler))
    try:
        yield
    finally:
        for model, name, handler in registered:
            event.remove(model, name, handler)
        feature_flag_manager._feature_flags = saved


def _query_tagged_object_table(session: Session) -> list[TaggedObject]:
    return session.query(TaggedObject).all()


def _clear_tagged_object_table(session: Session) -> None:
    session.query(TaggedObject).delete()
    session.commit()


@pytest.mark.usefixtures("with_tagging_system_feature")
def test_dataset_tagging(sync_session: Session):
    """Creating a dataset creates a corresponding tagged_object row."""
    _clear_tagged_object_table(sync_session)
    assert [] == _query_tagged_object_table(sync_session)

    test_dataset = SqlaTable(
        table_name="foo",
        schema=None,
        owners=[],
        database=get_main_database(),
        sql=None,
        extra='{"certification": 1}',
    )
    sync_session.add(test_dataset)
    sync_session.commit()

    tags = _query_tagged_object_table(sync_session)
    assert 1 == len(tags)
    assert "ObjectType.dataset" == str(tags[0].object_type)
    assert test_dataset.id == tags[0].object_id

    sync_session.delete(test_dataset)
    sync_session.commit()

    assert [] == _query_tagged_object_table(sync_session)


@pytest.mark.usefixtures("with_tagging_system_feature")
def test_chart_tagging(sync_session: Session):
    """Creating a chart creates a corresponding tagged_object row."""
    _clear_tagged_object_table(sync_session)
    assert [] == _query_tagged_object_table(sync_session)

    test_chart = Slice(
        slice_name="test_chart",
        datasource_type=DatasourceType.TABLE,
        viz_type="bubble",
        datasource_id=1,
    )
    sync_session.add(test_chart)
    sync_session.commit()

    tags = _query_tagged_object_table(sync_session)
    assert 1 == len(tags)
    assert "ObjectType.chart" == str(tags[0].object_type)
    assert test_chart.id == tags[0].object_id

    sync_session.delete(test_chart)
    sync_session.commit()

    assert [] == _query_tagged_object_table(sync_session)


@pytest.mark.usefixtures("with_tagging_system_feature")
def test_dashboard_tagging(sync_session: Session):
    """Creating a dashboard creates a corresponding tagged_object row."""
    _clear_tagged_object_table(sync_session)
    assert [] == _query_tagged_object_table(sync_session)

    test_dashboard = Dashboard()
    test_dashboard.dashboard_title = "test_dashboard"
    test_dashboard.slug = "test_slug"
    test_dashboard.published = True

    sync_session.add(test_dashboard)
    sync_session.commit()

    tags = _query_tagged_object_table(sync_session)
    assert 1 == len(tags)
    assert "ObjectType.dashboard" == str(tags[0].object_type)
    assert test_dashboard.id == tags[0].object_id

    sync_session.delete(test_dashboard)
    sync_session.commit()

    assert [] == _query_tagged_object_table(sync_session)


@pytest.mark.usefixtures("with_tagging_system_feature")
def test_saved_query_tagging(sync_session: Session):
    """Creating a saved query creates the type: tagged_object row.

    Upstream additionally emits an ``owner:None`` tag because
    ``QueryUpdater.get_owners_ids`` returns ``[target.user_id]`` unconditionally
    (i.e. ``[None]`` when ``user_id`` is unset), yielding 2 tags.  The Liteset
    port's ``_query_owners_ids`` returns ``[]`` when ``user_id is None`` and so
    skips the ``owner:None`` tag, producing only the ``type:query`` tag.  The
    upstream owner-tag assertions are retained below (commented) to document the
    single divergence; everything else is exercised 1:1.
    """
    _clear_tagged_object_table(sync_session)
    assert [] == _query_tagged_object_table(sync_session)

    test_saved_query = SavedQuery(label="test saved query")
    sync_session.add(test_saved_query)
    sync_session.commit()

    tags = _query_tagged_object_table(sync_session)

    # Upstream asserts 2 tags (owner:None + type:query); the port omits the
    # owner:None tag for a null user_id, so only the type:query tag is created.
    # assert "ObjectType.query" == str(tags[0].object_type)
    # assert "owner:None" == str(tags[0].tag.name)
    # assert "TagType.owner" == str(tags[0].tag.type)
    # assert test_saved_query.id == tags[0].object_id
    assert 1 == len(tags)

    assert "ObjectType.query" == str(tags[0].object_type)
    assert "type:query" == str(tags[0].tag.name)
    assert "TagType.type" == str(tags[0].tag.type)
    assert test_saved_query.id == tags[0].object_id

    sync_session.delete(test_saved_query)
    sync_session.commit()

    assert [] == _query_tagged_object_table(sync_session)


@pytest.mark.usefixtures("with_tagging_system_feature")
def test_favorite_tagging(sync_session: Session):
    """Favoriting an object creates a corresponding tagged_object row."""
    _clear_tagged_object_table(sync_session)
    assert [] == _query_tagged_object_table(sync_session)

    user = User(
        username="fav_tagging_user",
        first_name="fav",
        last_name="user",
        email="fav_tagging_user@example.com",
    )
    sync_session.add(user)
    sync_session.flush()

    test_saved_query = FavStar(user_id=user.id, class_name="slice", obj_id=1)
    sync_session.add(test_saved_query)
    sync_session.commit()

    tags = _query_tagged_object_table(sync_session)
    assert 1 == len(tags)
    assert "ObjectType.chart" == str(tags[0].object_type)
    assert test_saved_query.obj_id == tags[0].object_id

    sync_session.delete(test_saved_query)
    sync_session.commit()

    assert [] == _query_tagged_object_table(sync_session)

    sync_session.delete(user)
    sync_session.commit()


def test_tagging_system(sync_session: Session):
    """No tags are created when the TAGGING_SYSTEM feature flag is false.

    The listeners are not registered (the ``with_tagging_system_feature``
    fixture is intentionally NOT applied here), exactly mirroring upstream's
    ``@with_feature_flags(TAGGING_SYSTEM=False)``.
    """
    _clear_tagged_object_table(sync_session)
    assert [] == _query_tagged_object_table(sync_session)

    test_dataset = SqlaTable(
        table_name="foo",
        schema=None,
        owners=[],
        database=get_main_database(),
        sql=None,
        extra='{"certification": 1}',
    )

    test_chart = Slice(
        slice_name="test_chart",
        datasource_type=DatasourceType.TABLE,
        viz_type="bubble",
        datasource_id=1,
    )

    test_dashboard = Dashboard()
    test_dashboard.dashboard_title = "test_dashboard"
    test_dashboard.slug = "test_slug"
    test_dashboard.published = True

    test_saved_query = SavedQuery(label="test saved query")

    user = User(
        username="tagging_off_user",
        first_name="off",
        last_name="user",
        email="tagging_off_user@example.com",
    )
    sync_session.add(user)
    sync_session.flush()
    test_favorited_object = FavStar(user_id=user.id, class_name="slice", obj_id=1)

    sync_session.add(test_dataset)
    sync_session.add(test_chart)
    sync_session.add(test_dashboard)
    sync_session.add(test_saved_query)
    sync_session.add(test_favorited_object)
    sync_session.commit()

    tags = _query_tagged_object_table(sync_session)
    assert 0 == len(tags)

    sync_session.delete(test_dataset)
    sync_session.delete(test_chart)
    sync_session.delete(test_dashboard)
    sync_session.delete(test_saved_query)
    sync_session.delete(test_favorited_object)
    sync_session.commit()
    sync_session.delete(user)
    sync_session.commit()

    assert [] == _query_tagged_object_table(sync_session)
