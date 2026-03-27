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

from superset.db.base_dao import BaseAsyncDAO
from superset.models.annotations import Annotation, AnnotationLayer


class AsyncAnnotationLayerDAO(BaseAsyncDAO[AnnotationLayer]):
    model_cls = AnnotationLayer

    async def validate_update_uniqueness(
        self,
        name: str,
        layer_id: int | None = None,
    ) -> bool:
        """Check that annotation layer name is unique."""
        stmt = select(AnnotationLayer).where(AnnotationLayer.name == name)
        if layer_id is not None:
            stmt = stmt.where(AnnotationLayer.id != layer_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def has_annotations(
        self,
        model_id: int | list[int],
    ) -> bool:
        """Check if annotation layer(s) have any annotations."""
        if isinstance(model_id, list):
            stmt = (
                select(Annotation.id).where(Annotation.layer_id.in_(model_id)).limit(1)
            )
        else:
            stmt = select(Annotation.id).where(Annotation.layer_id == model_id).limit(1)
        result = await self.session.execute(stmt)
        return result.scalars().first() is not None


class AsyncAnnotationDAO(BaseAsyncDAO[Annotation]):
    model_cls = Annotation

    async def validate_update_uniqueness(
        self,
        layer_id: int,
        short_descr: str,
        annotation_id: int | None = None,
    ) -> bool:
        """Check annotation short description is unique within a layer."""
        stmt = select(Annotation).where(
            Annotation.short_descr == short_descr,
            Annotation.layer_id == layer_id,
        )
        if annotation_id is not None:
            stmt = stmt.where(Annotation.id != annotation_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None
