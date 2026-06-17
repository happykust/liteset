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
"""SQL validators registry.

Honours the ``SQL_VALIDATORS_BY_ENGINE`` config map (``{engine: name}``)
to dispatch a :class:`BaseSQLValidator` per engine. Engines without a
configured validator fall back to the sqlglot-based parser used by
:class:`ValidateSQLCommand`.
"""

from __future__ import annotations

from superset.sql.validators import base, postgres, presto_db
from superset.sql.validators.base import BaseSQLValidator, SQLValidationAnnotation


def get_validator_by_name(name: str) -> type[BaseSQLValidator] | None:
    """Return the validator class registered under ``name`` or ``None``.

    Only ``PrestoDBSQLValidator`` and ``PostgreSQLValidator`` are bundled.
    Custom validators may extend this map at runtime if needed.
    """
    return {
        "PrestoDBSQLValidator": presto_db.PrestoDBSQLValidator,
        "PostgreSQLValidator": postgres.PostgreSQLValidator,
    }.get(name)


__all__ = [
    "BaseSQLValidator",
    "SQLValidationAnnotation",
    "base",
    "get_validator_by_name",
    "postgres",
    "presto_db",
]
