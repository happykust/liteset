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
"""User-related models.

Pure SQLAlchemy -- no legacy WSGI dependencies.

Note: The ``User`` model itself is defined upstream in the
``ab_user`` table and is referenced by string (``"User"``) in
relationships throughout the codebase.  We do **not** re-define it here
to avoid duplicate mapper registrations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import backref, Mapped, mapped_column, relationship

from superset.models.helpers import AuditMixinNullable, Base

if TYPE_CHECKING:
    from superset.models.dashboard import Dashboard
    from superset.models.security import User


class UserAttribute(Base, AuditMixinNullable):
    """Extended user attributes (welcome dashboard, avatar, etc.)."""

    __tablename__ = "user_attribute"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ab_user.id"), nullable=True
    )
    welcome_dashboard_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("dashboards.id"), nullable=True
    )
    avatar_url: Mapped[str | None] = mapped_column(String(100))

    # -- relationships --------------------------------------------------------

    # ``backref="extra_attributes"`` exposes ``User.extra_attributes``
    # (required by the avatar endpoint).
    user: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[user_id],
        backref=backref("extra_attributes"),
    )
    welcome_dashboard: Mapped["Dashboard | None"] = relationship(
        "Dashboard",
        foreign_keys=[welcome_dashboard_id],
    )
