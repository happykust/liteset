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
"""Embedded dashboard model.

Pure SQLAlchemy -- no Flask dependencies.
"""
from __future__ import annotations

import uuid as uuid_mod
from sqlalchemy import Column, ForeignKey, Integer, Text
from sqlalchemy.orm import relationship
from sqlalchemy_utils import UUIDType

from liteset.models.helpers import AuditMixinNullable, Base


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class EmbeddedDashboard(Base, AuditMixinNullable):
    """An embedded dashboard configuration."""

    __tablename__ = "embedded_dashboards"

    uuid = Column(
        UUIDType(binary=True), default=uuid_mod.uuid4, primary_key=True
    )
    allow_domain_list = Column(Text)
    dashboard_id = Column(
        Integer,
        ForeignKey("dashboards.id", ondelete="CASCADE"),
        nullable=False,
    )

    # -- relationships --------------------------------------------------------

    dashboard = relationship(
        "Dashboard",
        foreign_keys=[dashboard_id],
        back_populates="embedded",
    )
