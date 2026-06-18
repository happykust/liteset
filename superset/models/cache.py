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
"""Cache key tracking model.

Pure SQLAlchemy -- no legacy WSGI dependencies.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from superset.models.helpers import Base

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class CacheKey(Base):
    """Tracks cache keys for invalidation purposes."""

    __tablename__ = "cache_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(256), nullable=False)
    cache_timeout: Mapped[int | None] = mapped_column(Integer, nullable=True)
    datasource_uid: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    created_on: Mapped[datetime | None] = mapped_column(
        DateTime, default=datetime.now, nullable=True
    )
