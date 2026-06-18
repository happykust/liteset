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
"""Dynamic plugin model.

Pure SQLAlchemy -- no legacy WSGI dependencies.
"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from superset.models.helpers import AuditMixinNullable, Base


class DynamicPlugin(Base, AuditMixinNullable):
    """A dynamically loaded visualization plugin."""

    __tablename__ = "dynamic_plugin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    bundle_url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)

    def __repr__(self) -> str:
        return str(self.name)
