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
"""Binary/text fixture-data helpers for integration tests."""

from __future__ import annotations

import json
from os import path
from typing import Any

FIXTURES_DIR = path.dirname(__file__)


def read_fixture(fixture_file_name: str) -> bytes:
    with open(path.join(FIXTURES_DIR, fixture_file_name), "rb") as fixture_file:
        return fixture_file.read()


def load_fixture(fixture_file_name: str) -> Any:
    return json.loads(read_fixture(fixture_file_name))
