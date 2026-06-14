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
"""Port of ``tests/unit_tests/databases/commands/test_connection_test.py``.

Verifies that ``get_log_connection_action`` builds the audit-log action
string by appending ``.<ExceptionClass>`` when an exception is present and
``.ssh_tunnel`` when a tunnel is present.
"""

from __future__ import annotations

from parameterized import parameterized

from superset.commands.database.test_connection import get_log_connection_action
from superset.models.ssh_tunnel import SSHTunnel


@parameterized.expand(
    [
        ("foo", None, None, "foo"),
        ("foo", SSHTunnel, None, "foo.ssh_tunnel"),
        ("foo", SSHTunnel, Exception("oops"), "foo.Exception.ssh_tunnel"),
    ],
)
def test_get_log_connection_action(action, tunnel, exc, expected_result):
    assert expected_result == get_log_connection_action(action, tunnel, exc)
