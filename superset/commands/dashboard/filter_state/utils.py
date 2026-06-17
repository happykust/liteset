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
"""Utilities for filter state access checks.

``check_access`` translates dashboard-layer exceptions into the
temporary-cache exception hierarchy:

* ``DashboardNotFoundError``  -> ``TemporaryCacheResourceNotFoundError``
* ``DashboardAccessDeniedError`` -> ``TemporaryCacheAccessDeniedError``

The DAO, security manager, and user are passed in explicitly rather than
pulled from request-scoped globals.
"""

from __future__ import annotations

from typing import Any

from superset.commands.dashboard.exceptions import (
    DashboardAccessDeniedError,
    DashboardNotFoundError,
)
from superset.commands.temporary_cache.exceptions import (
    TemporaryCacheAccessDeniedError,
    TemporaryCacheResourceNotFoundError,
)
from superset.exceptions import ForbiddenError, ObjectNotFoundError


async def check_access(
    dao: Any,
    resource_id: int,
    security_manager: Any | None = None,
    user: Any | None = None,
) -> None:
    """Ensure the current user can access dashboard ``resource_id``.

    Translates dashboard-layer errors into the temporary-cache hierarchy
    so the filter-state commands surface a consistent error contract.
    """
    try:
        # get_full_by_id_or_slug eager-loads owners/roles/slices so can_access_dashboard
        # doesn't fire a sync lazy-load (MissingGreenlet) on the async session.
        dashboard = await dao.get_full_by_id_or_slug(str(resource_id))
        if dashboard is None:
            raise DashboardNotFoundError()
        if (
            security_manager is not None
            and user is not None
            and hasattr(security_manager, "can_access_dashboard")
        ):
            allowed = await security_manager.can_access_dashboard(dashboard, user=user)
            if not allowed:
                raise DashboardAccessDeniedError()
    except DashboardNotFoundError as ex:
        raise TemporaryCacheResourceNotFoundError() from ex
    except DashboardAccessDeniedError as ex:
        raise TemporaryCacheAccessDeniedError() from ex
    except ObjectNotFoundError as ex:
        raise TemporaryCacheResourceNotFoundError() from ex
    except ForbiddenError as ex:
        raise TemporaryCacheAccessDeniedError() from ex
