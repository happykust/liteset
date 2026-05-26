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
"""SQL clause sanitization.

Thin compatibility wrapper that delegates to the canonical implementation in
``superset.sql.parse``. The original (``superset_old``) imports
``sanitize_clause`` directly from ``superset.sql.parse`` in both ``viz.py`` and
``common/query_object.py``; this module re-exports the same behavior so the new
callers (which import from here) keep the canonical normalization: SQL comments
are stripped via ``Dialect.generate(comments=False)`` and a
``QueryClauseValidationException`` is raised on invalid input.
"""

from __future__ import annotations

from superset.sql.parse import sanitize_clause as _sanitize_clause


def sanitize_clause(clause: str, engine: str = "postgresql") -> str:
    """Validate and normalize a SQL clause.

    Delegates to :func:`superset.sql.parse.sanitize_clause` so that the
    normalization (comment removal) and the raised exception type
    (``QueryClauseValidationException``) match the canonical implementation 1:1.
    """
    return _sanitize_clause(clause, engine)
