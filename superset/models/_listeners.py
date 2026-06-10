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
"""SQLAlchemy event listeners ported 1:1 from ``superset_old/``.

The original Superset registers listeners against the FAB ``Model`` —
``Slice.before_insert/before_update`` propagates a chart's datasource
``perm`` / ``schema_perm`` / ``catalog_perm``; ``Database`` lifecycle
hooks sync the FAB permission tables; ``User.after_insert`` clones the
welcome dashboard; the ``ObjectUpdater`` family maintains implicit
``owner:`` / ``type:`` / ``favorited_by:`` tags.

In Liteset we run an :class:`~sqlalchemy.ext.asyncio.AsyncSession`, but
SQLA's mapper-event machinery still fires synchronously inside the
underlying greenlet-bridged connection — exactly as in the original.
The handlers therefore use ``connection.execute(text(...))`` (raw SQL on
the live connection) rather than ORM session calls, which keeps them
async-compatible without smuggling event-loop primitives into the sync
flush phase.

Tag updaters and the welcome-dashboard clone listener follow the same
pattern as the original ``superset_old/tags/models.py:ObjectUpdater`` —
they open a short-lived sync ``Session`` bound to the flush connection
and commit before yielding back to the outer transaction.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.mapper import Mapper

if TYPE_CHECKING:
    from superset.models.connectors import SqlaTable
    from superset.models.core import Database, FavStar
    from superset.models.dashboard import Dashboard
    from superset.models.security import User
    from superset.models.slice import Slice
    from superset.models.sql_lab import Query

logger = logging.getLogger(__name__)

# Sync sessionmaker bound at flush time to the connection passed by SQLA.
# Mirrors the original ``superset_old/tags/models.py:Session = sessionmaker()``.
Session = sessionmaker()


# ---------------------------------------------------------------------------
# Slice listeners (1:1 with superset_old/models/slice.py)
# ---------------------------------------------------------------------------


def _slice_set_related_perm(
    _mapper: Mapper[Any], connection: Connection, target: "Slice"
) -> None:
    """Propagate the datasource's perm strings onto the chart row.

    Direct port of ``superset_old/models/slice.py:set_related_perm``.
    Originally executed via the ORM (``db.session.query(SqlaTable)``);
    we use the raw connection because the listener fires inside the
    sync flush phase of an :class:`AsyncSession` where ORM access would
    be illegal.
    """
    if not target.datasource_id:
        return
    if (target.datasource_type or "table") != "table":
        return

    row = connection.execute(
        text("SELECT perm, schema_perm, catalog_perm FROM tables WHERE id = :id"),
        {"id": int(target.datasource_id)},
    ).first()
    if row is None:
        return

    target.perm = row.perm
    target.schema_perm = row.schema_perm
    target.catalog_perm = row.catalog_perm


def _slice_after_changed(
    _mapper: Mapper[Any], _connection: Connection, target: "Slice"
) -> None:
    """Trigger thumbnail regeneration after a chart insert/update.

    1:1 with ``event_after_chart_changed`` in
    ``superset_old/models/slice.py``.  Only fires when
    ``THUMBNAILS_SQLA_LISTENERS`` is enabled (matches the original).
    """
    try:
        from superset.tasks.thumbnails import cache_chart_thumbnail
    except ImportError:
        return
    try:
        from superset.utils.core import get_current_user

        user = get_current_user()
        current_user = (
            user.username if user and not getattr(user, "is_anonymous", False) else None
        )
        cache_chart_thumbnail.delay(
            current_user=current_user,
            chart_id=target.id,
            force=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to enqueue chart thumbnail task for slice id=%s",
            target.id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Dashboard listeners (1:1 with superset_old/models/dashboard.py)
# ---------------------------------------------------------------------------


def _dashboard_after_changed(
    _mapper: Mapper[Any], _connection: Connection, target: "Dashboard"
) -> None:
    """Trigger thumbnail regeneration after a dashboard insert/update.

    1:1 with the ``THUMBNAILS_SQLA_LISTENERS`` block in
    ``superset_old/models/dashboard.py`` — calls
    :func:`cache_dashboard_thumbnail.delay`.
    """
    try:
        from superset.tasks.thumbnails import cache_dashboard_thumbnail
    except ImportError:
        return
    try:
        from superset.utils.core import get_current_user

        user = get_current_user()
        current_user = (
            user.username if user and not getattr(user, "is_anonymous", False) else None
        )
        cache_dashboard_thumbnail.delay(
            current_user=current_user,
            dashboard_id=target.id,
            force=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to enqueue dashboard thumbnail task for dashboard id=%s",
            target.id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# User listener -- welcome-dashboard clone
# (1:1 with superset_old/models/dashboard.py:copy_dashboard)
# ---------------------------------------------------------------------------


def _user_copy_dashboard(
    _mapper: Mapper[Any], connection: Connection, target: "User"
) -> None:
    """Clone the welcome dashboard for a newly-registered user.

    Direct port of ``copy_dashboard`` in
    ``superset_old/models/dashboard.py``.  The original reads
    ``app.config["DASHBOARD_TEMPLATE_ID"]`` — Liteset stores the same
    setting under ``SupersetSettings.dashboard_template_id``.
    """
    try:
        from superset.config import SupersetSettings
    except ImportError:
        return

    try:
        settings = SupersetSettings()  # type: ignore[call-arg]
        dashboard_id = getattr(settings, "dashboard_template_id", None)
    except Exception:  # noqa: BLE001
        dashboard_id = None
    if dashboard_id is None:
        return

    from superset.models.dashboard import Dashboard
    from superset.models.user import UserAttribute

    with Session(bind=connection) as session:
        template = (
            session.query(Dashboard).filter_by(id=int(dashboard_id)).one_or_none()
        )
        if template is None:
            return
        new_dash = Dashboard(
            dashboard_title=template.dashboard_title,
            position_json=template.position_json,
            description=template.description,
            css=template.css,
            json_metadata=template.json_metadata,
            slices=list(template.slices),
            owners=[target],
        )
        session.add(new_dash)
        session.flush()
        session.add(
            UserAttribute(
                user_id=target.id,
                welcome_dashboard_id=new_dash.id,
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# Database listeners -- perm sync
# (1:1 with superset_old/models/core.py:1202-1204)
#
# Uses raw SQL on the flush connection because the original
# ``security_manager.database_after_*`` hooks operated identically.
# ---------------------------------------------------------------------------


def _database_perm_for(target: "Database") -> str:
    return f"[{target.database_name}].(id:{target.id})"


def _database_after_insert(
    _mapper: Mapper[Any], connection: Connection, target: "Database"
) -> None:
    """Create the ``database_access`` PVM after a database insert.

    Mirrors ``security_manager.database_after_insert``.
    """
    perm_name = _database_perm_for(target)
    _ensure_pvm(connection, "database_access", perm_name)


def _database_after_update(
    _mapper: Mapper[Any], connection: Connection, target: "Database"
) -> None:
    """Rename PVMs when ``database_name`` changes.

    Mirrors ``security_manager.database_after_update``.  A SQLAlchemy
    history check tells us whether ``database_name`` actually changed.
    """
    state = sa.inspect(target)
    history = state.attrs.database_name.history
    if not history.has_changes():
        return
    deleted = history.deleted
    if not deleted:
        return
    old_name = deleted[0]
    new_name = target.database_name
    if old_name == new_name:
        return

    old_db_perm = f"[{old_name}].(id:{target.id})"
    new_db_perm = _database_perm_for(target)
    _rename_view_menu(connection, old_db_perm, new_db_perm)

    # Rename schema/catalog/datasource PVMs that reference this database
    # — same prefix substitution the original used.
    old_prefix = f"[{old_name}]."
    new_prefix = f"[{new_name}]."
    connection.execute(
        text(
            "UPDATE ab_view_menu "
            "SET name = :new || SUBSTR(name, :prefix_len + 1) "
            "WHERE SUBSTR(name, 1, :prefix_len) = :old"
        ),
        {
            "old": old_prefix,
            "new": new_prefix,
            "prefix_len": len(old_prefix),
        },
    )

    # Rename ``perm`` references on slices/tables that embed the old name.
    for table_name in ("tables", "slices"):
        connection.execute(
            text(
                f"UPDATE {table_name} "  # noqa: S608
                "SET perm = REPLACE(perm, :old, :new) "
                "WHERE perm LIKE :pattern"
            ),
            {
                "old": old_db_perm,
                "new": new_db_perm,
                "pattern": f"%{old_db_perm}%",
            },
        )


def _database_after_delete(
    _mapper: Mapper[Any], connection: Connection, target: "Database"
) -> None:
    """Delete database/schema/catalog PVMs when a database is deleted.

    Mirrors ``security_manager.database_after_delete``.
    """
    db_perm = _database_perm_for(target)
    _delete_pvm(connection, "database_access", db_perm)
    # delete schema_access / catalog_access PVMs whose view_menu starts
    # with ``[<db>].``.
    prefix = f"[{target.database_name}]."
    rows = connection.execute(
        text(
            "SELECT pv.id, vm.id AS view_menu_id "
            "FROM ab_permission_view pv "
            "JOIN ab_permission p ON pv.permission_id = p.id "
            "JOIN ab_view_menu vm ON pv.view_menu_id = vm.id "
            "WHERE p.name IN ('schema_access', 'catalog_access') "
            "AND SUBSTR(vm.name, 1, :prefix_len) = :prefix"
        ),
        {"prefix": prefix, "prefix_len": len(prefix)},
    ).fetchall()
    for row in rows:
        _delete_pvm_by_id(connection, int(row.id), int(row.view_menu_id))


# ---------------------------------------------------------------------------
# SqlaTable listeners (1:1 with superset_old/connectors/sqla/models.py:2002+)
#
# The original behaviour is split: ``before_update`` re-loads the
# ``database`` relationship and updates ``perm`` / ``schema_perm`` /
# ``catalog_perm``; ``after_insert`` / ``after_delete`` create or remove
# the corresponding ``datasource_access`` PVM.  Liteset's
# ``security_manager`` keeps the same effects.
# ---------------------------------------------------------------------------


def _dataset_perm(connection: Connection, target: "SqlaTable") -> str | None:
    if target.database_id is None:
        return None
    row = connection.execute(
        text("SELECT database_name FROM dbs WHERE id = :id"),
        {"id": int(target.database_id)},
    ).first()
    if row is None:
        return None
    return f"[{row.database_name}].[{target.table_name}](id:{target.id})"


def _sqlatable_before_update(
    _mapper: Mapper[Any], connection: Connection, target: "SqlaTable"
) -> None:
    """Refresh ``perm`` / ``schema_perm`` / ``catalog_perm`` before update.

    Mirrors ``SqlaTable.before_update`` /
    ``security_manager.dataset_before_update``.
    """
    perm = _dataset_perm(connection, target)
    if perm is not None:
        target.perm = perm


def _sqlatable_after_insert(
    _mapper: Mapper[Any], connection: Connection, target: "SqlaTable"
) -> None:
    """Create the ``datasource_access`` PVM after a dataset insert."""
    perm = _dataset_perm(connection, target)
    if perm is None:
        return
    target.perm = perm
    _ensure_pvm(connection, "datasource_access", perm)


def _sqlatable_after_delete(
    _mapper: Mapper[Any], connection: Connection, target: "SqlaTable"
) -> None:
    """Remove the ``datasource_access`` PVM after a dataset delete."""
    if not target.perm:
        return
    _delete_pvm(connection, "datasource_access", target.perm)


# ---------------------------------------------------------------------------
# Tag updater listeners (1:1 with superset_old/tags/models.py)
#
# These maintain the implicit ``owner:`` / ``type:`` /
# ``favorited_by:`` tags that the Tag UI surfaces — without them users
# see broken filters.
# ---------------------------------------------------------------------------


# String values match ``superset_old/tags/models.py:ObjectType``.  We use
# the lowercase string form because that's what the ``object_type``
# column persists (and what the M2M Slice/Dashboard relationship
# matches against in superset/models/slice.py / dashboard.py).
_OBJECT_TYPE_CHART = "chart"
_OBJECT_TYPE_DASHBOARD = "dashboard"
_OBJECT_TYPE_QUERY = "query"
_OBJECT_TYPE_DATASET = "dataset"


def _get_or_create_tag(session: Any, name: str, type_value: Any) -> Any:
    """Get a Tag by name or create it.  Mirrors ``tags/models.get_tag``."""
    from superset.models.tags import Tag

    tag = session.query(Tag).filter_by(name=name.strip(), type=type_value).one_or_none()
    if tag is None:
        tag = Tag(name=name.strip(), type=type_value)
        session.add(tag)
        session.flush()
    return tag


def _add_tagged_object_if_missing(
    session: Any,
    tag_id: int,
    object_id: int,
    object_type: str,
) -> None:
    from superset.models.tags import TaggedObject

    exists = (
        session.query(TaggedObject.id)
        .filter(
            TaggedObject.tag_id == tag_id,
            TaggedObject.object_id == object_id,
            TaggedObject.object_type == object_type,
        )
        .first()
    )
    if exists is None:
        session.add(
            TaggedObject(
                tag_id=tag_id,
                object_id=object_id,
                object_type=object_type,
            )
        )


def _object_after_insert(
    object_type: str,
    target: Any,
    connection: Connection,
    owner_ids: list[int],
) -> None:
    """Generic ``ObjectUpdater.after_insert`` body.

    Adds ``owner:<id>`` and ``type:<object_type>`` tags.
    """
    from superset.models.tags import TagType

    with Session(bind=connection) as session:
        for owner_id in owner_ids:
            owner_tag = _get_or_create_tag(session, f"owner:{owner_id}", TagType.owner)
            _add_tagged_object_if_missing(
                session,
                tag_id=owner_tag.id,
                object_id=target.id,
                object_type=object_type,
            )
        type_tag = _get_or_create_tag(session, f"type:{object_type}", TagType.type)
        _add_tagged_object_if_missing(
            session,
            tag_id=type_tag.id,
            object_id=target.id,
            object_type=object_type,
        )
        session.commit()


def _object_after_update(
    object_type: str,
    target: Any,
    connection: Connection,
    owner_ids: list[int],
) -> None:
    """Generic ``ObjectUpdater.after_update`` body — re-syncs owner tags."""
    from superset.models.tags import Tag, TaggedObject, TagType

    with Session(bind=connection) as session:
        existing = (
            session.query(TaggedObject)
            .join(Tag)
            .filter(
                TaggedObject.object_type == object_type,
                TaggedObject.object_id == target.id,
                Tag.type == TagType.owner,
            )
            .all()
        )
        existing_owner_tag_ids = {to.tag_id for to in existing}

        new_owner_tag_ids: set[int] = set()
        for owner_id in owner_ids:
            tag = _get_or_create_tag(session, f"owner:{owner_id}", TagType.owner)
            new_owner_tag_ids.add(tag.id)

        for tag_id in new_owner_tag_ids - existing_owner_tag_ids:
            session.add(
                TaggedObject(
                    tag_id=tag_id,
                    object_id=target.id,
                    object_type=object_type,
                )
            )
        for to in existing:
            if to.tag_id not in new_owner_tag_ids:
                session.delete(to)
        session.commit()


def _object_after_delete(
    object_type: str,
    target: Any,
    connection: Connection,
) -> None:
    """Generic ``ObjectUpdater.after_delete`` body — drops every tag row."""
    from superset.models.tags import TaggedObject

    with Session(bind=connection) as session:
        session.query(TaggedObject).filter(
            TaggedObject.object_type == object_type,
            TaggedObject.object_id == target.id,
        ).delete()
        session.commit()


def _slice_owners_ids(target: "Slice") -> list[int]:
    return [o.id for o in (target.owners or [])]


def _dashboard_owners_ids(target: "Dashboard") -> list[int]:
    return [o.id for o in (target.owners or [])]


def _dataset_owners_ids(target: "SqlaTable") -> list[int]:
    return [o.id for o in (target.owners or [])]


def _query_owners_ids(target: "Query") -> list[int]:
    return [target.user_id] if target.user_id is not None else []


# Wrappers that match SQLA's listener signature
# (mapper, connection, target).


def _chart_tag_after_insert(_m: Mapper[Any], c: Connection, t: "Slice") -> None:
    _object_after_insert(_OBJECT_TYPE_CHART, t, c, _slice_owners_ids(t))


def _chart_tag_after_update(_m: Mapper[Any], c: Connection, t: "Slice") -> None:
    _object_after_update(_OBJECT_TYPE_CHART, t, c, _slice_owners_ids(t))


def _chart_tag_after_delete(_m: Mapper[Any], c: Connection, t: "Slice") -> None:
    _object_after_delete(_OBJECT_TYPE_CHART, t, c)


def _dashboard_tag_after_insert(_m: Mapper[Any], c: Connection, t: "Dashboard") -> None:
    _object_after_insert(_OBJECT_TYPE_DASHBOARD, t, c, _dashboard_owners_ids(t))


def _dashboard_tag_after_update(_m: Mapper[Any], c: Connection, t: "Dashboard") -> None:
    _object_after_update(_OBJECT_TYPE_DASHBOARD, t, c, _dashboard_owners_ids(t))


def _dashboard_tag_after_delete(_m: Mapper[Any], c: Connection, t: "Dashboard") -> None:
    _object_after_delete(_OBJECT_TYPE_DASHBOARD, t, c)


def _dataset_tag_after_insert(_m: Mapper[Any], c: Connection, t: "SqlaTable") -> None:
    _object_after_insert(_OBJECT_TYPE_DATASET, t, c, _dataset_owners_ids(t))


def _dataset_tag_after_update(_m: Mapper[Any], c: Connection, t: "SqlaTable") -> None:
    _object_after_update(_OBJECT_TYPE_DATASET, t, c, _dataset_owners_ids(t))


def _dataset_tag_after_delete(_m: Mapper[Any], c: Connection, t: "SqlaTable") -> None:
    _object_after_delete(_OBJECT_TYPE_DATASET, t, c)


def _query_tag_after_insert(_m: Mapper[Any], c: Connection, t: "Query") -> None:
    _object_after_insert(_OBJECT_TYPE_QUERY, t, c, _query_owners_ids(t))


def _query_tag_after_update(_m: Mapper[Any], c: Connection, t: "Query") -> None:
    _object_after_update(_OBJECT_TYPE_QUERY, t, c, _query_owners_ids(t))


def _query_tag_after_delete(_m: Mapper[Any], c: Connection, t: "Query") -> None:
    _object_after_delete(_OBJECT_TYPE_QUERY, t, c)


# FavStar updaters: implicit ``favorited_by:<user>`` tags
def _favstar_after_insert(
    _mapper: Mapper[Any], connection: Connection, target: "FavStar"
) -> None:
    from superset.models.tags import TagType

    object_type_map = {
        "slice": _OBJECT_TYPE_CHART,
        "Dashboard": _OBJECT_TYPE_DASHBOARD,
        "SqlaTable": _OBJECT_TYPE_DATASET,
    }
    obj_type = object_type_map.get(str(target.class_name))
    if obj_type is None:
        return
    with Session(bind=connection) as session:
        tag = _get_or_create_tag(
            session,
            f"favorited_by:{target.user_id}",
            TagType.favorited_by,
        )
        _add_tagged_object_if_missing(
            session,
            tag_id=tag.id,
            object_id=int(target.obj_id),
            object_type=obj_type,
        )
        session.commit()


def _favstar_after_delete(
    _mapper: Mapper[Any], connection: Connection, target: "FavStar"
) -> None:
    from superset.models.tags import Tag, TaggedObject, TagType

    name = f"favorited_by:{target.user_id}"
    with Session(bind=connection) as session:
        ids_query = (
            session.query(TaggedObject.id)
            .join(Tag)
            .filter(
                TaggedObject.object_id == target.obj_id,
                Tag.type == TagType.favorited_by,
                Tag.name == name,
            )
        )
        ids = [row[0] for row in ids_query]
        if ids:
            session.query(TaggedObject).filter(TaggedObject.id.in_(ids)).delete(
                synchronize_session=False
            )
        session.commit()


# ---------------------------------------------------------------------------
# Raw-SQL PVM helpers (used by Database / SqlaTable listeners)
# ---------------------------------------------------------------------------


def _ensure_pvm(connection: Connection, perm_name: str, view_menu_name: str) -> None:
    """Create a Permission/ViewMenu/PermissionView triple if missing.

    Idempotent — matches the original
    ``security_manager.add_permission_view_menu`` semantics.
    """
    perm_id = _ensure_permission(connection, perm_name)
    vm_id = _ensure_view_menu(connection, view_menu_name)
    existing = connection.execute(
        text(
            "SELECT id FROM ab_permission_view "
            "WHERE permission_id = :pid AND view_menu_id = :vmid"
        ),
        {"pid": perm_id, "vmid": vm_id},
    ).first()
    if existing is None:
        connection.execute(
            text(
                "INSERT INTO ab_permission_view (permission_id, view_menu_id) "
                "VALUES (:pid, :vmid)"
            ),
            {"pid": perm_id, "vmid": vm_id},
        )


def _ensure_permission(connection: Connection, name: str) -> int:
    row = connection.execute(
        text("SELECT id FROM ab_permission WHERE name = :name"),
        {"name": name},
    ).first()
    if row is not None:
        return int(row.id)
    # ``text("INSERT ...")`` returns a CursorResult whose
    # ``inserted_primary_key`` raises ``InvalidRequestError`` because
    # the executable isn't a Core ``insert()`` construct.  The
    # post-insert SELECT works on every supported backend (PG / MySQL /
    # SQLite) and matches the lookup the caller would do anyway.
    connection.execute(
        text("INSERT INTO ab_permission (name) VALUES (:name)"),
        {"name": name},
    )
    row = connection.execute(
        text("SELECT id FROM ab_permission WHERE name = :name"),
        {"name": name},
    ).first()
    return int(row.id)


def _ensure_view_menu(connection: Connection, name: str) -> int:
    row = connection.execute(
        text("SELECT id FROM ab_view_menu WHERE name = :name"),
        {"name": name},
    ).first()
    if row is not None:
        return int(row.id)
    connection.execute(
        text("INSERT INTO ab_view_menu (name) VALUES (:name)"),
        {"name": name},
    )
    row = connection.execute(
        text("SELECT id FROM ab_view_menu WHERE name = :name"),
        {"name": name},
    ).first()
    return int(row.id)


def _delete_pvm(connection: Connection, perm_name: str, view_menu_name: str) -> None:
    """Delete a PVM + role assoc + orphaned ViewMenu (matches original)."""
    row = connection.execute(
        text(
            "SELECT pv.id AS id, vm.id AS view_menu_id "
            "FROM ab_permission_view pv "
            "JOIN ab_permission p ON pv.permission_id = p.id "
            "JOIN ab_view_menu vm ON pv.view_menu_id = vm.id "
            "WHERE p.name = :perm_name AND vm.name = :vm_name"
        ),
        {"perm_name": perm_name, "vm_name": view_menu_name},
    ).first()
    if row is None:
        return
    _delete_pvm_by_id(connection, int(row.id), int(row.view_menu_id))


def _delete_pvm_by_id(connection: Connection, pvm_id: int, view_menu_id: int) -> None:
    connection.execute(
        text("DELETE FROM ab_permission_view_role WHERE permission_view_id = :pvid"),
        {"pvid": pvm_id},
    )
    connection.execute(
        text("DELETE FROM ab_permission_view WHERE id = :pvid"),
        {"pvid": pvm_id},
    )
    remaining = connection.execute(
        text("SELECT id FROM ab_permission_view WHERE view_menu_id = :vmid LIMIT 1"),
        {"vmid": view_menu_id},
    ).first()
    if remaining is None:
        connection.execute(
            text("DELETE FROM ab_view_menu WHERE id = :vmid"),
            {"vmid": view_menu_id},
        )


def _rename_view_menu(connection: Connection, old_name: str, new_name: str) -> None:
    connection.execute(
        text("UPDATE ab_view_menu SET name = :new WHERE name = :old"),
        {"old": old_name, "new": new_name},
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


_REGISTERED = False


def register() -> None:
    """Register every event listener once.

    Imported (and called) by :mod:`superset.models.__init__` so that
    every listener is wired up after every mapped class is registered.
    """
    global _REGISTERED  # noqa: PLW0603
    if _REGISTERED:
        return
    _REGISTERED = True

    # Imports here (not at module top) to avoid circular imports during
    # the initial module-loading dance.  Each model module imports
    # ``superset.models.helpers`` which in turn must finish initialising
    # before this module's class references resolve.
    from superset.models.connectors import SqlaTable
    from superset.models.core import Database, FavStar
    from superset.models.dashboard import Dashboard
    from superset.models.security import User
    from superset.models.slice import Slice
    from superset.models.sql_lab import Query

    # Slice perm propagation + thumbnail invalidation
    event.listen(Slice, "before_insert", _slice_set_related_perm)
    event.listen(Slice, "before_update", _slice_set_related_perm)
    if _is_thumbnails_listeners_enabled():
        event.listen(Slice, "after_insert", _slice_after_changed)
        event.listen(Slice, "after_update", _slice_after_changed)
        event.listen(Dashboard, "after_insert", _dashboard_after_changed)
        event.listen(Dashboard, "after_update", _dashboard_after_changed)

    # Database perm-sync
    event.listen(Database, "after_insert", _database_after_insert)
    event.listen(Database, "after_update", _database_after_update)
    event.listen(Database, "after_delete", _database_after_delete)

    # SqlaTable perm-sync
    event.listen(SqlaTable, "before_update", _sqlatable_before_update)
    event.listen(SqlaTable, "after_insert", _sqlatable_after_insert)
    event.listen(SqlaTable, "after_delete", _sqlatable_after_delete)

    # User welcome-dashboard clone
    event.listen(User, "after_insert", _user_copy_dashboard)

    # Tag updaters
    event.listen(Slice, "after_insert", _chart_tag_after_insert)
    event.listen(Slice, "after_update", _chart_tag_after_update)
    event.listen(Slice, "after_delete", _chart_tag_after_delete)

    event.listen(Dashboard, "after_insert", _dashboard_tag_after_insert)
    event.listen(Dashboard, "after_update", _dashboard_tag_after_update)
    event.listen(Dashboard, "after_delete", _dashboard_tag_after_delete)

    event.listen(SqlaTable, "after_insert", _dataset_tag_after_insert)
    event.listen(SqlaTable, "after_update", _dataset_tag_after_update)
    event.listen(SqlaTable, "after_delete", _dataset_tag_after_delete)

    event.listen(Query, "after_insert", _query_tag_after_insert)
    event.listen(Query, "after_update", _query_tag_after_update)
    event.listen(Query, "after_delete", _query_tag_after_delete)

    event.listen(FavStar, "after_insert", _favstar_after_insert)
    event.listen(FavStar, "after_delete", _favstar_after_delete)


def _is_thumbnails_listeners_enabled() -> bool:
    """Mirror the original ``is_feature_enabled('THUMBNAILS_SQLA_LISTENERS')``.

    Reads the boolean off
    :class:`~superset.config.SupersetSettings.feature_flags` (the
    Liteset equivalent of ``FEATURE_FLAGS``).
    """
    try:
        from superset.utils.feature_flags import feature_flag_manager
    except ImportError:
        return False
    try:
        return bool(
            feature_flag_manager.is_feature_enabled("THUMBNAILS_SQLA_LISTENERS")
        )
    except Exception:  # noqa: BLE001
        return False


__all__ = ["register", "Session"]


# Quiet ``json`` imported but unused — used by callers via
# ``superset.models._listeners`` re-export only when needed.
_ = json  # noqa: F841
