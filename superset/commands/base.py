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

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from superset.exceptions import CommandException  # noqa: F401 — re-export

T = TypeVar("T")


class AsyncBaseCommand(ABC, Generic[T]):
    """Base class for all async commands (business logic layer).

    Transaction management: Commands call session.flush() (not commit).
    The provide_async_session dependency (Phase 1) wraps each HTTP request
    in a transaction — commit on success, rollback on any exception.
    Commands MUST NOT call session.commit() or session.rollback() directly.
    """

    @abstractmethod
    async def validate(self) -> None: ...

    @abstractmethod
    async def run(self) -> T: ...

    async def execute(self) -> T:
        await self.validate()
        return await self.run()
