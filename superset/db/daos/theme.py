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
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select

from superset.db.base_dao import BaseAsyncDAO
from superset.models.core import Theme

logger = logging.getLogger(__name__)


class AsyncThemeDAO(BaseAsyncDAO[Theme]):
    model_cls = Theme

    async def find_by_uuid(self, uuid_str: str) -> Theme | None:
        """Find a theme by UUID."""
        try:
            uuid_val = UUID(uuid_str)
        except ValueError:
            return None
        return await self.find_one_or_none(uuid=uuid_val)

    async def find_system_default(self) -> Theme | None:
        """Find the system default theme (``is_system_default=True``),
        falling back to the ``THEME_DEFAULT`` system theme."""
        stmt = select(Theme).where(Theme.is_system_default.is_(True))
        result = await self.session.execute(stmt)
        system_defaults = list(result.scalars().all())

        if len(system_defaults) == 1:
            return system_defaults[0]

        if len(system_defaults) > 1:
            logger.warning(
                "Multiple system default themes found (%d), "
                "falling back to config theme",
                len(system_defaults),
            )

        stmt = select(Theme).where(
            Theme.is_system.is_(True),
            Theme.theme_name == "THEME_DEFAULT",
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def find_system_dark(self) -> Theme | None:
        """Find the system dark theme (``is_system_dark=True``),
        falling back to the ``THEME_DARK`` system theme."""
        stmt = select(Theme).where(Theme.is_system_dark.is_(True))
        result = await self.session.execute(stmt)
        system_darks = list(result.scalars().all())

        if len(system_darks) == 1:
            return system_darks[0]

        if len(system_darks) > 1:
            logger.warning(
                "Multiple system dark themes found (%d), falling back to config theme",
                len(system_darks),
            )

        stmt = select(Theme).where(
            Theme.is_system.is_(True),
            Theme.theme_name == "THEME_DARK",
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()
