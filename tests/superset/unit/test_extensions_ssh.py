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
"""Native Liteset port of the upstream SSHManagerFactory unit tests.

Flask-free: no flask, flask_appbuilder or werkzeug.
"""

from __future__ import annotations

import sshtunnel

from superset.extensions.ssh import SSHManagerFactory


def test_ssh_tunnel_timeout_setting() -> None:
    # The Liteset ``SSHManager`` reads its knobs straight off the supplied
    # config object via ``_resolve_setting`` (which falls back to ``.get()``
    # for the legacy ``UPPER_CASE`` keys), so a plain mapping stands in for the
    # upstream ``app.config``.
    config = {
        "SSH_TUNNEL_MAX_RETRIES": 2,
        "SSH_TUNNEL_LOCAL_BIND_ADDRESS": "test",
        "SSH_TUNNEL_TIMEOUT_SEC": 123.0,
        "SSH_TUNNEL_PACKET_TIMEOUT_SEC": 321.0,
        "SSH_TUNNEL_MANAGER_CLASS": "superset.extensions.ssh.SSHManager",
    }
    factory = SSHManagerFactory()
    factory.init_app(config)
    assert sshtunnel.TUNNEL_TIMEOUT == 123.0
    assert sshtunnel.SSH_TIMEOUT == 321.0
