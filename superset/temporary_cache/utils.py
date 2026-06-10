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
"""1:1 port of ``superset_old/temporary_cache/utils.py``.

``cache_key`` builds the user-visible cache key strings used by the
filter-state and explore-form-data temporary caches.  The string format
must stay identical to upstream (``";"``-joined) so that entries written
by an upstream Superset instance resolve after migrating to liteset
(the metastore cache derives the row UUID as uuid3(namespace, this string)).
"""

from typing import Any

SEPARATOR = ";"


def cache_key(*args: Any) -> str:
    return SEPARATOR.join(str(arg) for arg in args)
