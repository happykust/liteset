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
"""Legacy (v0) dashboard / dataset importers.

These modules implement the original unversioned bundle formats:

* :mod:`superset.importexport.legacy.dashboard_v0` — JSON-encoded
  dashboards exported by the pre-1.0 ``superset export_dashboards`` CLI.
* :mod:`superset.importexport.legacy.dataset_v0` — YAML-encoded datasets
  exported by the pre-1.0 ``superset export_datasources`` CLI.

The new code path uses the v1 ZIP bundle format
(:mod:`superset.commands.importers.v1.assets`), but legacy bundles
remain in the wild —
:class:`~superset.importexport.legacy.dispatcher.ImportDashboardsCommand`
and
:class:`~superset.importexport.legacy.dispatcher.ImportDatasetsCommand`
provide a back-compat dispatcher that tries v1 first and falls back to
v0 (matching the original ``commands/{dashboard,dataset}/importers/dispatcher.py``).
"""

from superset.importexport.legacy.dashboard_v0 import (
    ImportDashboardsCommand as V0ImportDashboardsCommand,
)
from superset.importexport.legacy.dataset_v0 import (
    ImportDatasetsCommand as V0ImportDatasetsCommand,
)
from superset.importexport.legacy.dispatcher import (
    ImportDashboardsCommand,
    ImportDatasetsCommand,
)

__all__ = [
    "ImportDashboardsCommand",
    "ImportDatasetsCommand",
    "V0ImportDashboardsCommand",
    "V0ImportDatasetsCommand",
]
