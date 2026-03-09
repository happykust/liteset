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

from sqlalchemy import select

from liteset.db.base_dao import BaseAsyncDAO
from superset.models.core import Log


class AsyncLogDAO(BaseAsyncDAO[Log]):
    model_cls = Log

    async def get_recent_activity(
        self,
        user_id: int,
        actions: list[str],
        page: int = 0,
        page_size: int = 25,
    ) -> list[Log]:
        """Get recent activity logs for a user filtered by actions."""
        stmt = (
            select(Log)
            .where(
                Log.user_id == user_id,
                Log.action.in_(actions),
            )
            .order_by(Log.dttm.desc())
            .offset(page * page_size)
            .limit(page_size)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
