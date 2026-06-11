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
"""Legacy (v0) dashboard importer.

Direct port of ``superset_old/commands/dashboard/importers/v0.py``.

A v0 dashboard bundle is a single JSON document with two keys:

* ``datasources`` — list of ``SqlaTable`` objects (encoded with
  ``object_hook=decode_dashboards``);
* ``dashboards`` — list of ``Dashboard`` objects, each carrying its
  ``slices`` collection.

Imports run synchronously against a regular SQLAlchemy ``Session``
because v0 imports are exclusively driven from CLI / Celery contexts.
The legacy ``ImportExportMixin`` helpers (``alter_params``,
``params_dict``, ``override``, ``copy``, ``reset_ownership``,
``remove_params``) are reimplemented locally to keep the new
``superset/models/`` API free of legacy methods while still supporting
v0 round-trips.
"""

from __future__ import annotations

import logging
import time
from copy import copy
from datetime import datetime
from typing import Any

from sqlalchemy.orm import make_transient

from superset.exceptions import DashboardImportException
from superset.i18n import _ as _gettext
from superset.importexport.legacy.dataset_v0 import import_dataset
from superset.migrations.shared.native_filters import migrate_dashboard
from superset.utils import json
from superset.utils.dashboard_filter_scopes_converter import (
    convert_filter_scopes,
    copy_filter_scopes,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local re-implementations of the legacy ImportExportMixin helpers.
# ---------------------------------------------------------------------------


def _params_dict(obj: Any) -> dict[str, Any]:
    """Return ``params`` parsed as a dict.

    Mirrors :func:`ImportExportMixin.params_dict` from the original
    code.  Falls back to an empty dict for missing / malformed JSON,
    matching :func:`superset.models.helpers.json_to_dict` semantics.
    """
    if hasattr(obj, "params_dict"):
        try:
            return obj.params_dict
        except Exception:  # noqa: BLE001, S110
            pass
    raw = getattr(obj, "params", None) or "{}"
    try:
        return json.loads(raw) or {}
    except (TypeError, ValueError):
        return {}


def _alter_params(obj: Any, **kwargs: Any) -> None:
    """Mirror :func:`ImportExportMixin.alter_params` 1:1."""
    params = _params_dict(obj)
    params.update(kwargs)
    obj.params = json.dumps(params)


def _remove_params(obj: Any, param_to_remove: str) -> None:
    """Mirror :func:`ImportExportMixin.remove_params` 1:1."""
    params = _params_dict(obj)
    params.pop(param_to_remove, None)
    obj.params = json.dumps(params)


def _override_export_fields(target: Any, source: Any) -> None:
    """Mirror :func:`ImportExportMixin.override` 1:1."""
    for field in source.__class__.export_fields:
        setattr(target, field, getattr(source, field))


def _copy_export_fields(obj: Any) -> Any:
    """Mirror :func:`ImportExportMixin.copy` 1:1."""
    new_obj = obj.__class__()
    _override_export_fields(new_obj, obj)
    return new_obj


def _reset_ownership(obj: Any, user: Any | None = None) -> None:
    """Mirror :func:`ImportExportMixin.reset_ownership` 1:1.

    Owners default to ``[user]`` when supplied so the new dashboard
    belongs to the importing user; otherwise ownership is left to be
    populated by the caller (mirroring the original ``g.user`` fallback).
    """
    obj.created_by = None
    obj.changed_by = None
    obj.owners = []
    if user is not None:
        obj.owners = [user]


def _current_orm_user(session: Any) -> Any | None:
    """Resolve the importing user as an ORM row bound to ``session``.

    Mirrors the ``if g and hasattr(g, "user"): self.owners = [g.user]``
    fallback in the original ``ImportExportMixin.reset_ownership``
    (superset_old/models/helpers.py:448-457).  The liteset ContextVar may
    hold a ``CachedUser`` (not ORM-mapped), so re-fetch by id.
    """
    from superset.utils.core import get_current_user

    user_id = getattr(get_current_user(), "id", None)
    if user_id is None:
        return None
    from superset.models.security import User

    return session.get(User, user_id)


def _get_datasource_by_name(
    session: Any,
    datasource_name: str,
    database_name: str,
    catalog: str | None,
    schema: str | None,
) -> Any | None:
    """Resolve a :class:`SqlaTable` by name, mirroring the original helper.

    Mirrors ``SqlaTable.get_datasource_by_name`` in
    ``superset_old/connectors/sqla/models.py``.
    """
    from superset.models.connectors import SqlaTable
    from superset.models.core import Database

    # 1:1 with the original: the catalog filter ALWAYS applies (None →
    # ``WHERE catalog IS NULL``), and the schema match is done in Python so
    # '' and NULL compare equal across dialects
    # (superset_old/connectors/sqla/models.py:1218-1238).
    schema = schema or None
    query = (
        session.query(SqlaTable)
        .join(Database)
        .filter(SqlaTable.table_name == datasource_name)
        .filter(Database.database_name == database_name)
        .filter(SqlaTable.catalog == catalog)
    )
    for tbl in query.all():
        if schema == (tbl.schema or None):
            return tbl
    return None


# ---------------------------------------------------------------------------
# Slice & dashboard import helpers
# ---------------------------------------------------------------------------


def import_chart(
    session: Any,
    slc_to_import: Any,
    slc_to_override: Any | None,
    import_time: int | None = None,
) -> int:
    """Insert or override a slice. Returns the resulting id."""
    make_transient(slc_to_import)
    slc_to_import.dashboards = []
    _alter_params(
        slc_to_import,
        remote_id=slc_to_import.id,
        import_time=import_time,
    )

    slc_to_import = _copy_export_fields(slc_to_import)
    _reset_ownership(slc_to_import, user=_current_orm_user(session))
    params = _params_dict(slc_to_import)
    datasource = _get_datasource_by_name(
        session,
        datasource_name=params["datasource_name"],
        database_name=params["database_name"],
        catalog=params.get("catalog"),
        schema=params.get("schema"),
    )
    slc_to_import.datasource_id = datasource.id  # type: ignore[union-attr]
    if slc_to_override:
        _override_export_fields(slc_to_override, slc_to_import)
        session.flush()
        return slc_to_override.id
    session.add(slc_to_import)
    session.flush()
    return slc_to_import.id


def import_dashboard(  # noqa: C901
    session: Any,
    dashboard_to_import: Any,
    dataset_id_mapping: dict[int, int] | None = None,
    import_time: int | None = None,
) -> int:
    """Import the dashboard from the object into the database.

    Once imported, ``json_metadata`` is extended to store ``remote_id``
    and ``import_time`` so the next import can decide between override
    and create.  Slices belonging to the dashboard are wired to existing
    tables.  Audit metadata is intentionally not copied over.
    """
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    def alter_positions(dashboard: Any, old_to_new_slc_id_dict: dict[int, int]) -> None:
        """Update ``slice_id`` references in ``position_json``."""
        position_data = json.loads(dashboard.position_json)
        position_json = position_data.values()
        for value in position_json:
            if (
                isinstance(value, dict)
                and value.get("meta")
                and value.get("meta", {}).get("chartId")
            ):
                old_slice_id = value["meta"]["chartId"]
                if old_slice_id in old_to_new_slc_id_dict:
                    value["meta"]["chartId"] = old_to_new_slc_id_dict[old_slice_id]
        dashboard.position_json = json.dumps(position_data)

    def alter_native_filters(dashboard: Any) -> None:
        json_metadata = json.loads(dashboard.json_metadata)
        native_filter_configuration = json_metadata.get("native_filter_configuration")
        if not native_filter_configuration:
            return
        for native_filter in native_filter_configuration:
            for target in native_filter.get("targets", []):
                old_dataset_id = target.get("datasetId")
                if dataset_id_mapping and old_dataset_id is not None:
                    target["datasetId"] = dataset_id_mapping.get(
                        old_dataset_id,
                        old_dataset_id,
                    )
        dashboard.json_metadata = json.dumps(json_metadata)

    logger.info("Started import of the dashboard: %s", dashboard_to_import)
    logger.info("Dashboard has %d slices", len(dashboard_to_import.slices))
    # Copy slices because the per-slice import mutates the slice's
    # dashboard collection.
    slices = copy(dashboard_to_import.slices)

    # Clear the slug to avoid uniqueness conflicts on re-import.
    dashboard_to_import.slug = None

    old_json_metadata = json.loads(dashboard_to_import.json_metadata or "{}")
    old_to_new_slc_id_dict: dict[int, int] = {}
    new_timed_refresh_immune_slices: list[str] = []
    new_expanded_slices: dict[str, Any] = {}
    new_filter_scopes: dict[str, Any] = {}
    i_params_dict = _params_dict(dashboard_to_import)
    remote_id_slice_map = {
        _params_dict(slc)["remote_id"]: slc
        for slc in session.query(Slice).all()
        if "remote_id" in _params_dict(slc)
    }
    new_slice_ids: list[int] = []
    for slc in slices:
        logger.info(
            "Importing slice %s from the dashboard: %s",
            getattr(slc, "slice_name", ""),
            getattr(dashboard_to_import, "dashboard_title", ""),
        )
        remote_slc = remote_id_slice_map.get(slc.id)
        new_slc_id = import_chart(session, slc, remote_slc, import_time=import_time)
        new_slice_ids.append(new_slc_id)
        old_to_new_slc_id_dict[slc.id] = new_slc_id
        # update json metadata that deals with slice ids
        new_slc_id_str = str(new_slc_id)
        old_slc_id_str = str(slc.id)
        if (
            "timed_refresh_immune_slices" in i_params_dict
            and old_slc_id_str in i_params_dict["timed_refresh_immune_slices"]
        ):
            new_timed_refresh_immune_slices.append(new_slc_id_str)
        if (
            "expanded_slices" in i_params_dict
            and old_slc_id_str in i_params_dict["expanded_slices"]
        ):
            new_expanded_slices[new_slc_id_str] = i_params_dict["expanded_slices"][
                old_slc_id_str
            ]

    # Since PR #9109, ``filter_immune_slices`` and
    # ``filter_immune_slice_fields`` are converted to ``filter_scopes``;
    # legacy bundles still carry the old keys, which are translated
    # forward here.
    # Single ``filter_scopes`` variable through all three phases — 1:1 with
    # upstream v0.py:210-225 (a split ``converted_scopes`` name caused
    # NameError for bundles carrying ``filter_scopes`` without the legacy
    # ``filter_immune_*`` keys, and passed the wrong scopes when both were
    # present).
    # ``Any``-typed: phase 2 holds the int-keyed convert_filter_scopes
    # output, phase 3 the str-keyed metadata dict (upstream is untyped here).
    filter_scopes: Any = {}
    if (
        "filter_immune_slices" in i_params_dict
        or "filter_immune_slice_fields" in i_params_dict
    ):
        filter_scopes = convert_filter_scopes(old_json_metadata, slices)

    if "filter_scopes" in i_params_dict:
        filter_scopes = old_json_metadata.get("filter_scopes")

    if filter_scopes:
        new_filter_scopes = copy_filter_scopes(
            old_to_new_slc_id_dict=old_to_new_slc_id_dict,
            old_filter_scopes=filter_scopes,
        )

    # Override the dashboard if it already exists (matching by remote_id).
    existing_dashboard = None
    for dash in session.query(Dashboard).all():
        if (
            "remote_id" in _params_dict(dash)
            and _params_dict(dash)["remote_id"] == dashboard_to_import.id
        ):
            existing_dashboard = dash

    dashboard_to_import = _copy_export_fields(dashboard_to_import)
    dashboard_to_import.id = None
    _reset_ownership(dashboard_to_import, user=_current_orm_user(session))
    # ``position_json`` may be empty for dashboards built only via the
    # chart-edit page without re-arranging.
    if dashboard_to_import.position_json:
        alter_positions(dashboard_to_import, old_to_new_slc_id_dict)
    _alter_params(dashboard_to_import, import_time=import_time)
    _remove_params(dashboard_to_import, "filter_immune_slices")
    _remove_params(dashboard_to_import, "filter_immune_slice_fields")
    if new_filter_scopes:
        _alter_params(dashboard_to_import, filter_scopes=new_filter_scopes)
    if new_expanded_slices:
        _alter_params(dashboard_to_import, expanded_slices=new_expanded_slices)
    if new_timed_refresh_immune_slices:
        _alter_params(
            dashboard_to_import,
            timed_refresh_immune_slices=new_timed_refresh_immune_slices,
        )

    alter_native_filters(dashboard_to_import)

    if existing_dashboard:
        _override_export_fields(existing_dashboard, dashboard_to_import)
    else:
        session.add(dashboard_to_import)

    dashboard = existing_dashboard or dashboard_to_import
    dashboard.slices = (
        session.query(Slice).filter(Slice.id.in_(old_to_new_slc_id_dict.values())).all()
    )
    # Migrate any filter-box charts to native dashboard filters.
    migrate_dashboard(dashboard)
    session.flush()
    return dashboard.id


def decode_dashboards(o: dict[str, Any]) -> Any:
    """JSON ``object_hook`` that reconstructs models from the legacy export.

    Mirrors ``superset_old/commands/dashboard/importers/v0.py:decode_dashboards``.
    """
    from superset.models.connectors import SqlaTable, SqlMetric, TableColumn
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    if "__Dashboard__" in o:
        return Dashboard(**o["__Dashboard__"])
    if "__Slice__" in o:
        return Slice(**o["__Slice__"])
    if "__TableColumn__" in o:
        return TableColumn(**o["__TableColumn__"])
    if "__SqlaTable__" in o:
        return SqlaTable(**o["__SqlaTable__"])
    if "__SqlMetric__" in o:
        return SqlMetric(**o["__SqlMetric__"])
    if "__datetime__" in o:
        return datetime.strptime(o["__datetime__"], "%Y-%m-%dT%H:%M:%S")
    return o


def import_dashboards(
    session: Any,
    content: str,
    database_id: int | None = None,
    import_time: int | None = None,
) -> None:
    """Import dashboards from a JSON-encoded stream into the metadata DB."""
    current_tt = int(time.time())
    import_time = current_tt if import_time is None else import_time
    data = json.loads(content, object_hook=decode_dashboards)
    if not data:
        raise DashboardImportException(_gettext("No data in file"))
    dataset_id_mapping: dict[int, int] = {}
    for table in data["datasources"]:
        new_dataset_id = import_dataset(
            session,
            table,
            database_id,
            import_time=import_time,
        )
        params = json.loads(table.params)
        dataset_id_mapping[params["remote_id"]] = new_dataset_id

    for dashboard in data["dashboards"]:
        import_dashboard(
            session,
            dashboard,
            dataset_id_mapping,
            import_time=import_time,
        )


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------


class ImportDashboardsCommand:
    """Import dashboards in legacy JSON format.

    Direct port of
    ``superset_old/commands/dashboard/importers/v0.py:ImportDashboardsCommand``.
    """

    # pylint: disable=unused-argument
    def __init__(
        self,
        contents: dict[str, str],
        database_id: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.contents = contents
        self.database_id = database_id

    def run(self, session: Any | None = None) -> None:
        """Validate and execute the import.

        ``session`` defaults to the result of
        :func:`superset.db.session.get_sync_session` — supply one to run
        the import inside an existing transaction (CLI / tests).
        """
        self.validate()

        if session is None:
            from superset.db.session import get_sync_session

            session = get_sync_session()
            owns_session = True
        else:
            owns_session = False

        try:
            for file_name, content in self.contents.items():
                logger.info("Importing dashboard from file %s", file_name)
                import_dashboards(session, content, self.database_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def validate(self) -> None:
        """Ensure every file is JSON before any import is attempted.

        1:1 with the original v0 command (superset_old/commands/dashboard/
        importers/v0.py:340-347): a malformed JSON re-raises the ValueError
        bare. Converting it to IncorrectVersionError would make the
        dispatcher silently skip this version and report the misleading
        "Could not find a valid command to import file" instead of the real
        JSON parse error.
        """
        for _file_name, content in self.contents.items():
            try:
                json.loads(content)
            except ValueError:
                logger.exception("Invalid JSON file")
                raise


__all__ = [
    "ImportDashboardsCommand",
    "decode_dashboards",
    "import_chart",
    "import_dashboard",
    "import_dashboards",
]
