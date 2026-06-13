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
"""Analytics DB connection-safety guard.

Ported 1:1 from
``superset_old/security/analytics_db_safety.py``.

The chart importer (:mod:`superset.commands.chart.importers.v1.utils`)
calls :func:`check_sqlalchemy_uri` against every DB URI in an imported
bundle.  Drivers that allow filesystem access from inside a SQL query
(``sqlite``, ``shillelagh``) are rejected because the analytics DB
runs with a service-account that should not be able to read arbitrary
local files.  When the ``ENABLE_SUPERSET_META_DB`` feature flag is
disabled the meta-DB driver is also rejected.

This module is intentionally framework-agnostic — the original Apache
Superset version pulls in the upstream ``lazy_gettext`` for the error
message; Liteset substitutes the no-op shim from
:mod:`superset.i18n` so we can run without the legacy WSGI stack.
"""

from __future__ import annotations

import re

from sqlalchemy.engine.url import URL
from sqlalchemy.exc import NoSuchModuleError

from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import SupersetSecurityException
from superset.i18n import gettext as _
from superset.utils.feature_flags import feature_flag_manager

# list of unsafe SQLAlchemy dialects
BLOCKLIST = {
    # sqlite creates a local DB, which allows mapping server's filesystem
    re.compile(r"sqlite(?:\+[^\s]*)?$"),
    # shillelagh allows opening local files (eg, 'SELECT * FROM "csv:///etc/passwd"')
    re.compile(r"shillelagh$"),
    re.compile(r"shillelagh\+apsw$"),
}


def check_sqlalchemy_uri(uri: URL) -> None:
    """Reject SQLAlchemy URIs that would let analytics queries escape to
    the local filesystem (sqlite, shillelagh) or cross the meta-DB
    boundary when ``ENABLE_SUPERSET_META_DB`` is disabled.

    Mirrors :func:`superset_old.security.analytics_db_safety.check_sqlalchemy_uri`
    1:1 — same dialect inspection, same error type, same message.

    Raises:
        SupersetSecurityException: when the URI's drivername matches
            one of the blocklisted regexes.
    """
    if not feature_flag_manager.is_feature_enabled("ENABLE_SUPERSET_META_DB"):
        BLOCKLIST.add(re.compile(r"superset$"))

    for blocklist_regex in BLOCKLIST:
        if not re.match(blocklist_regex, uri.drivername):
            continue
        try:
            dialect = uri.get_dialect().__name__
        except (NoSuchModuleError, ValueError):
            dialect = uri.drivername

        raise SupersetSecurityException(
            SupersetError(
                error_type=SupersetErrorType.DATABASE_SECURITY_ACCESS_ERROR,
                message=_(
                    "%(dialect)s cannot be used as a data source for security reasons.",
                    dialect=dialect,
                ),
                level=ErrorLevel.ERROR,
            )
        )
