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

import logging
from typing import Any

from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class AsyncUserDAO:
    """Async DAO for user operations.

    Does not inherit BaseAsyncDAO because the User model class depends
    on Flask-AppBuilder's security manager configuration and is resolved
    at runtime rather than being a fixed class attribute.
    """

    def __init__(self, session: AsyncSession, user_model: type | None = None) -> None:
        self.session = session
        self._user_model = user_model

    @property
    def user_model(self) -> type:
        if self._user_model is None:
            try:
                from superset.extensions import security_manager

                self._user_model = security_manager.user_model
            except ImportError as err:
                raise RuntimeError(
                    "User model not available. Pass user_model to constructor."
                ) from err
        return self._user_model

    async def get_by_id(self, user_id: int) -> Any | None:
        """Get a user by ID."""
        return await self.session.get(self.user_model, user_id)

    async def set_avatar_url(self, user: Any, avatar_url: str) -> None:
        """Set the avatar URL for a user.

        Updates the user's extra attributes with the avatar URL.
        """
        # Refresh to safely load lazy relationship in async context
        try:
            await self.session.refresh(user, attribute_names=["extra_attributes"])
        except (InvalidRequestError, AttributeError):
            logger.debug("User model has no extra_attributes relationship")

        extra_attributes = getattr(user, "extra_attributes", [])
        if extra_attributes and hasattr(extra_attributes[0], "avatar_url"):
            extra_attributes[0].avatar_url = avatar_url
        else:
            try:
                from liteset.models.user import UserAttribute

                attr = UserAttribute(
                    user_id=user.id,
                    avatar_url=avatar_url,
                )
                self.session.add(attr)
            except ImportError:
                logger.debug("UserAttribute model not available")
