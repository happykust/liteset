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
"""Annotation models: AnnotationLayer, Annotation.

Pure SQLAlchemy -- no Flask dependencies.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    MediumText,
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AnnotationLayer(Base, AuditMixinNullable):
    """A logical grouping of annotations."""

    __tablename__ = "annotation_layer"

    id = Column(Integer, primary_key=True)
    name = Column(String(250))
    descr = Column(Text)


class Annotation(Base, AuditMixinNullable):
    """A single annotation within a layer."""

    __tablename__ = "annotation"
    __table_args__ = (Index("ti_dag_state", "layer_id", "start_dttm", "end_dttm"),)

    id = Column(Integer, primary_key=True)
    start_dttm = Column(DateTime)
    end_dttm = Column(DateTime)
    layer_id = Column(Integer, ForeignKey("annotation_layer.id"), nullable=True)
    short_descr = Column(String(500))
    long_descr = Column(Text)
    json_metadata = Column(MediumText())

    # -- relationships --------------------------------------------------------

    layer = relationship(
        "AnnotationLayer",
        foreign_keys=[layer_id],
        backref="annotation",
    )
