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

from uuid import UUID

from liteset.db.base_dao import BaseAsyncDAO
from liteset.db.daos.favorites_mixin import FavoriteMixin
from superset.models.core import FavStarClassName
from superset.models.slice import Slice


class AsyncChartDAO(FavoriteMixin, BaseAsyncDAO[Slice]):
    model_cls = Slice
    _fav_class_name = FavStarClassName.CHART

    async def get_by_id_or_uuid(self, id_or_uuid: int | str) -> Slice | None:
        """Find a chart by integer ID or UUID string."""
        try:
            chart_id = int(id_or_uuid)
            return await self.find_by_id(chart_id)
        except (ValueError, TypeError):
            pass

        # Try UUID lookup
        try:
            uuid_val = UUID(str(id_or_uuid))
        except ValueError:
            return None

        return await self.find_one_or_none(uuid=uuid_val)
