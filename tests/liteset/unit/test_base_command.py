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

import pytest

from liteset.commands.base import AsyncBaseCommand, CommandException


class SuccessCommand(AsyncBaseCommand[str]):
    async def validate(self) -> None:
        pass

    async def run(self) -> str:
        return "success"


class FailValidationCommand(AsyncBaseCommand[str]):
    async def validate(self) -> None:
        raise CommandException("validation failed")

    async def run(self) -> str:
        return "should not reach"


async def test_execute_success() -> None:
    cmd = SuccessCommand()
    result = await cmd.execute()
    assert result == "success"


async def test_execute_validation_failure() -> None:
    cmd = FailValidationCommand()
    with pytest.raises(CommandException, match="validation failed"):
        await cmd.execute()


async def test_cannot_instantiate_abstract() -> None:
    with pytest.raises(TypeError):
        AsyncBaseCommand()  # type: ignore[abstract]
