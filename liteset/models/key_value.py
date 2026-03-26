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
"""Key-value entry model for the key-value store.

Pure SQLAlchemy -- no Flask dependencies.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String

from liteset.models.helpers import AuditMixinNullable, Base, ImportExportMixin


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class KeyValueEntry(AuditMixinNullable, ImportExportMixin, Base):
    """A key-value entry used by the key-value store subsystem.

    Note: ``created_on``, ``created_by_fk``, ``changed_on``, and
    ``changed_by_fk`` are declared explicitly here (overriding the mixin)
    because the key-value store requires precise control over these columns.
    """

    __tablename__ = "key_value"

    id = Column(Integer, primary_key=True)
    resource = Column(String(32), nullable=False)
    value = Column(
        LargeBinary(length=2**24 - 1), nullable=False
    )

    # Explicit audit columns (override AuditMixinNullable defaults)
    created_on = Column(DateTime, nullable=True)
    created_by_fk = Column(
        Integer, ForeignKey("ab_user.id"), nullable=True
    )
    changed_on = Column(DateTime, nullable=True)
    changed_by_fk = Column(
        Integer, ForeignKey("ab_user.id"), nullable=True
    )
    expires_on = Column(DateTime, nullable=True)
