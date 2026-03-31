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
"""Compatibility shim: re-exports from superset.utils.date + DateRangeMigration."""

# Re-export functions that already exist in superset.utils.date
from superset.utils.date import get_since_until, parse_human_timedelta  # noqa: F401


class DateRangeMigration:
    x_dateunit_in_since = (
        r'"time_range":\s*"\s*[0-9]+\s+(day|week|month|quarter|year)s?\s*\s:\s'
    )
    x_dateunit_in_until = (
        r'"time_range":\s*".*\s:\s*[0-9]+\s+(day|week|month|quarter|year)s?\s*"'
    )
    x_dateunit = r"^\s*[0-9]+\s+(day|week|month|quarter|year)s?\s*$"
