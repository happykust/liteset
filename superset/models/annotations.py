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
"""Annotation models: AnnotationLayer, Annotation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    MediumText,
)


class AnnotationLayer(Base, AuditMixinNullable):
    """A logical grouping of annotations."""

    __tablename__ = "annotation_layer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(250))
    descr: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return str(self.name)


class Annotation(Base, AuditMixinNullable):
    """A single annotation within a layer."""

    __tablename__ = "annotation"
    __table_args__ = (Index("ti_dag_state", "layer_id", "start_dttm", "end_dttm"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_dttm: Mapped[datetime | None] = mapped_column(DateTime)
    end_dttm: Mapped[datetime | None] = mapped_column(DateTime)
    layer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("annotation_layer.id"), nullable=False
    )
    short_descr: Mapped[str | None] = mapped_column(String(500))
    long_descr: Mapped[str | None] = mapped_column(Text)
    json_metadata: Mapped[str | None] = mapped_column(MediumText())

    def __repr__(self) -> str:
        return str(self.short_descr)

    @property
    def data(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "start_dttm": self.start_dttm,
            "end_dttm": self.end_dttm,
            "short_descr": self.short_descr,
            "long_descr": self.long_descr,
            "layer": self.layer.name if self.layer else None,
        }

    layer: Mapped["AnnotationLayer"] = relationship(
        "AnnotationLayer",
        foreign_keys=[layer_id],
        backref="annotation",
    )
