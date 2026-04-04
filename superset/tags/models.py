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
"""Tag enums re-exported from superset.models.tags for convenience.

The canonical ORM models (Tag, TaggedObject) live in ``superset.models.tags``.
This module re-exports the enums so that existing ``from superset.tags.models
import TagType, ObjectType`` imports keep working.
"""

from superset.models.tags import (
    ObjectType,
    Tag,
    TaggedObject,
    TagType,
    user_favorite_tag_table,
)

__all__ = [
    "ObjectType",
    "Tag",
    "TaggedObject",
    "TagType",
    "user_favorite_tag_table",
]
