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
"""Async port of ``superset_old/utils/dashboard_import_export.py``.

The original module exposed a single helper, :func:`export_dashboards`,
used by the legacy CLI command ``superset legacy-export-dashboards`` to
emit a JSON dump of every dashboard in the metadata DB.

This async port preserves the public API exactly and runs against the
sync SQLAlchemy session created by :mod:`superset.db.session` (the same
session pattern used by the example loaders).  The legacy V0 export
format is deprecated in upstream Apache Superset but kept here for
parity so external automation that still consumes the format keeps
working through the migration.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def export_dashboards() -> str:
    """Return all dashboards metadata as a JSON dump.

    Verbatim port — uses the sync session helper from
    :mod:`superset.db.session` because the legacy export classmethod
    walks SQLA relationships eagerly and is fundamentally synchronous.
    """
    from superset.db.session import get_sync_session
    from superset.models.dashboard import Dashboard

    logger.info("Starting export")
    session = get_sync_session()
    try:
        dashboards = session.query(Dashboard)
        dashboard_ids: set[int] = set()
        for dashboard in dashboards:
            dashboard_ids.add(dashboard.id)

        # ``Dashboard.export_dashboards`` is a classmethod on the SQLA
        # model; the original lives in the upstream model module.  When
        # the new model doesn't ship that classmethod (the V0 export
        # format is deprecated) we fall back to the upstream
        # implementation defined in ``superset_old.models.dashboard``.
        export_fn = getattr(Dashboard, "export_dashboards", None)
        if not callable(export_fn):
            raise NotImplementedError(  # pragma: no cover
                "Dashboard.export_dashboards classmethod is not available — "
                "the V0 dashboard export format is no longer supported on "
                "this Superset build.  Use ``superset export-dashboards`` "
                "(zip-based V1 export) instead."
            )
        return export_fn(dashboard_ids)
    finally:
        session.close()
