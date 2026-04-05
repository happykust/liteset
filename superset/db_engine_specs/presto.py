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
"""Presto engine spec -- sync/Flask-compatible.

Ported 1:1 from ``superset_old/db_engine_specs/presto.py`` with Flask
imports removed.  Only the concrete ``PrestoEngineSpec`` class is included
here; the abstract ``PrestoBaseEngineSpec`` used by both Presto and Trino
lives in ``superset/db_engine_specs/trino.py``.
"""

from __future__ import annotations

from superset.db_engine_specs.trino import PrestoBaseEngineSpec


class PrestoEngineSpec(PrestoBaseEngineSpec):
    engine = "presto"
    engine_name = "Presto"

    allows_alias_to_source_column = False


__all__ = [
    "PrestoEngineSpec",
]
