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
"""Re-export of ``check_access`` for explore_form_data commands.

The original Superset places ``check_access`` in ``superset.explore.utils``
and both the explore_form_data and explore_permalink command modules
import it from there.  Here we keep a single canonical implementation
in ``commands.explore_permalink.utils`` and re-export it for
explore_form_data callers — matches the original 1:1 control flow without
duplicating the logic.
"""

from __future__ import annotations

from superset.commands.explore_permalink.utils import (
    check_access,
    check_dataset_access,
    check_datasource_access,
    check_query_access,
)

__all__ = [
    "check_access",
    "check_dataset_access",
    "check_datasource_access",
    "check_query_access",
]
