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
"""Superset SQLAlchemy models.

All models previously in superset/models/ are now here.
Backward-compatible re-exports exist in superset/models/ during
the transition period.
"""

# Import ALL model modules so that string-referenced classes
# (User, Role, SqlaTable, etc.) are registered in the mapper registry
# before any cross-module relationships are resolved.
import superset.models.annotations  # noqa: F401
import superset.models.cache  # noqa: F401
import superset.models.connectors  # noqa: F401
import superset.models.core  # noqa: F401
import superset.models.dashboard  # noqa: F401
import superset.models.dynamic_plugins  # noqa: F401
import superset.models.embedded_dashboard  # noqa: F401
import superset.models.key_value  # noqa: F401
import superset.models.reports  # noqa: F401
import superset.models.security  # noqa: F401
import superset.models.slice  # noqa: F401
import superset.models.sql_lab  # noqa: F401
import superset.models.tags  # noqa: F401
import superset.models.user  # noqa: F401

# Wire synchronous event listeners once all model classes are registered.
# This must come after all model imports above so mapper references
# (Slice, Dashboard, SqlaTable, etc.) are fully resolved.
from superset.models._listeners import register as _register_listeners  # noqa: E402

_register_listeners()
