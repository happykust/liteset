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
"""Async port of ``superset_old/commands/dashboard/filter_state/utils.py``.

The original ``check_access`` is module-level, takes a single ``resource_id``
and translates the dashboard-existence / dashboard-access exceptions raised
by ``DashboardDAO.get_by_id_or_slug`` into the temporary-cache exception
hierarchy:

* ``DashboardNotFoundError``  -> ``TemporaryCacheResourceNotFoundError``
* ``DashboardAccessDeniedError`` -> ``TemporaryCacheAccessDeniedError``

This async port mirrors the same control flow against the AsyncDashboardDAO
and the AsyncSecurityManager.  The DAO + security manager + user are passed
in (rather than pulled from Flask globals) because Liteset uses explicit
DI for everything.
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

    1:1 with ``superset_old.commands.dashboard.filter_state.utils.check_access``
    — translates dashboard-layer errors into the temporary-cache hierarchy
    so the filter-state commands surface a consistent error contract.
    """
    try:
        # Eager-load the relationships ``can_access_dashboard``/``is_owner``
        # touch (owners, roles, slices). Plain ``get_by_id_or_slug`` returns a
        # bare row which then triggers MissingGreenlet when the security
        # manager pokes at ``dashboard.owners`` on the AsyncSession.
        dashboard = await dao.get_full_by_id_or_slug(str(resource_id))
        if dashboard is None:
            # The original raises ``DashboardNotFoundError`` from inside
            # ``DashboardDAO.get_by_id_or_slug`` when the row is missing;
            # the async DAO returns ``None`` instead, so synthesize the
            # same exception here for parity.
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
        # ``ObjectNotFoundError`` is the cross-cutting Liteset variant of
        # "thing missing" — translate to the same temporary-cache exception
        # the original code surfaced for ``DashboardNotFoundError``.
        raise TemporaryCacheResourceNotFoundError() from ex
    except ForbiddenError as ex:
        raise TemporaryCacheAccessDeniedError() from ex
