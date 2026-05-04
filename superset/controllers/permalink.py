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
"""Permalink controllers — re-exports for import-path compatibility.

The dashboard permalink endpoints are merged into
``superset.controllers.dashboard`` (``DashboardController``).
The explore permalink endpoints live in
``superset.controllers.explore_permalink`` (``ExplorePermalinkController``).
The SQL Lab permalink endpoints live in
``superset.controllers.sqllab_permalink`` (``SqllabPermalinkController``).

This module exists so that ``import superset.controllers.permalink``
succeeds for any tooling that references the old path.
"""

from __future__ import annotations
