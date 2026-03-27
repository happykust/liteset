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
"""SQL clause sanitization."""
from __future__ import annotations

import sqlglot

from superset.exceptions import SupersetValidationException


def sanitize_clause(clause: str, engine: str = "postgresql") -> str:
    """Validate and normalize a SQL clause using sqlglot."""
    try:
        parsed = sqlglot.parse_one(clause, read=engine)
        return parsed.sql(dialect=engine)
    except sqlglot.errors.ParseError as exc:
        raise SupersetValidationException(
            f"Invalid SQL clause: {clause}"
        ) from exc
