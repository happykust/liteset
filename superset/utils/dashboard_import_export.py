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
"""Dashboard export helpers for the legacy V0 export format.

Exposes a single helper, :func:`export_dashboards`, used by the legacy
CLI command ``superset legacy-export-dashboards`` to emit a JSON dump of
every dashboard in the metadata DB.

Runs against the sync SQLAlchemy session created by
:mod:`superset.db.session` (the same session pattern used by the example
loaders).  The legacy V0 export format is deprecated but kept here for
external automation that still consumes it.
"""

from __future__ import annotations

import logging
from typing import Any, cast

logger = logging.getLogger(__name__)


def export_dashboards() -> str:
    """Return all dashboards metadata as a JSON dump.

    Uses the sync session helper from :mod:`superset.db.session` because
    the legacy export classmethod walks SQLA relationships eagerly and is
    fundamentally synchronous.
    """
    from superset.db.session import get_sync_session
    from superset.models.dashboard import Dashboard

    logger.info("Starting export")
    session = get_sync_session()
    try:
        dashboard_ids = {dashboard.id for dashboard in session.query(Dashboard)}
        return _export_dashboards(session, cast("set[int]", dashboard_ids))
    finally:
        session.close()


def _export_dashboards(session: Any, dashboard_ids: set[int]) -> str:
    """Export dashboards in the V0 format.

    Kept as a module function (rather than a model classmethod) so the
    async Dashboard model isn't burdened with a sync, deprecated export
    path.  ``SqlaTable.get_eager_sqlatable_datasource`` is inlined as a
    direct query since that classmethod isn't available here.
    """
    from sqlalchemy.orm import subqueryload

    from superset.models.connectors import SqlaTable
    from superset.models.dashboard import Dashboard
    from superset.utils import json

    copied_dashboards = []
    datasource_ids = set()
    for dashboard_id in dashboard_ids:
        dashboard_id = int(dashboard_id)
        dashboard = (
            session.query(Dashboard)
            .options(subqueryload(Dashboard.slices))
            .filter_by(id=dashboard_id)
            .first()
        )
        # remove ids and relations (like owners, created by, slices, ...)
        copied_dashboard = dashboard.copy()
        for slc in dashboard.slices:
            datasource_ids.add((slc.datasource_id, slc.datasource_type))
            copied_slc = slc.copy()
            # save original id into json — needed to update the dashboard's
            # json metadata on import
            copied_slc.id = slc.id
            copied_slc.alter_params(
                remote_id=slc.id,
                datasource_name=slc.datasource.datasource_name,
                schema=slc.datasource.schema,
                database_name=slc.datasource.database.name,
            )
            # set slices without creating ORM relations
            slices = copied_dashboard.__dict__.setdefault("slices", [])
            slices.append(copied_slc)

        json_metadata = json.loads(dashboard.json_metadata or "{}")
        native_filter_configuration = json_metadata.get(
            "native_filter_configuration", []
        )
        for native_filter in native_filter_configuration:
            for target in native_filter.get("targets", []):
                id_ = target.get("datasetId")
                if id_ is None:
                    continue
                datasource = session.query(SqlaTable).filter_by(id=int(id_)).first()
                if datasource is not None:
                    datasource_ids.add((datasource.id, datasource.type))

        copied_dashboard.alter_params(remote_id=dashboard_id)
        copied_dashboards.append(copied_dashboard)

    datasource_id_list = sorted(datasource_ids)

    eager_datasources = []
    for datasource_id, _ in datasource_id_list:
        eager_datasource = (
            session.query(SqlaTable)
            .options(
                subqueryload(SqlaTable.columns),
                subqueryload(SqlaTable.metrics),
            )
            .filter_by(id=datasource_id)
            .first()
        )
        if eager_datasource is None:
            # A dashboard may reference a dataset id that no longer exists
            # (dangling native-filter datasetId); skip it rather than crash on
            # ``None.copy()``.
            continue
        copied_datasource = eager_datasource.copy()
        copied_datasource.alter_params(
            remote_id=eager_datasource.id,
            database_name=eager_datasource.database.name,
        )
        eager_datasources.append(copied_datasource)

    # ``default=None`` so simplejson uses ``DashboardEncoder.default`` (which
    # serialises ORM objects via ``__dict__``) instead of the wrapper's
    # ``json_iso_dttm_ser`` param-default — the latter wins when both ``cls``
    # and ``default`` are passed and would raise on a Dashboard instance.
    return json.dumps(
        {"dashboards": copied_dashboards, "datasources": eager_datasources},
        default=None,
        cls=json.DashboardEncoder,
        indent=4,
    )
