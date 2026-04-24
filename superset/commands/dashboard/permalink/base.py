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
"""Async port of ``superset_old/commands/dashboard/permalink/base.py``.

The base class binds the per-resource ``KeyValueResource`` and the
``SharedKey`` used to look up the install-wide hashids salt that encodes
permalink ids into URL-friendly strings.

In the original Apache Superset the codec is a ``MarshmallowKeyValueCodec``
wrapping ``DashboardPermalinkSchema``.  Liteset has dropped Marshmallow
in favour of msgspec / plain JSON, so the codec is implemented inside the
concrete create/get commands using ``json.dumps`` / ``json.loads`` —
matching the on-disk format the original schema produced.
"""

from __future__ import annotations

from abc import ABC

from sqlalchemy.ext.asyncio import AsyncSession

from superset.commands.base import AsyncBaseCommand
from superset.key_value.shared_entries import get_permalink_salt
from superset.key_value.types import KeyValueResource, SharedKey


class BaseDashboardPermalinkCommand(AsyncBaseCommand, ABC):  # type: ignore[type-arg]
    """Common base for dashboard permalink commands.

    Async port of
    ``superset_old.commands.dashboard.permalink.base.BaseDashboardPermalinkCommand``.
    """

    resource = KeyValueResource.DASHBOARD_PERMALINK

    async def _salt(self, session: AsyncSession) -> str:
        """Look up (and lazily seed) the install-wide permalink salt."""
        return await get_permalink_salt(session, SharedKey.DASHBOARD_PERMALINK_SALT)
