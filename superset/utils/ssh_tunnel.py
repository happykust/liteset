#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.

"""SSH tunnel helpers — ported 1:1 from
``superset_old/utils/ssh_tunnel.py`` to the Liteset runtime.

The original module exposed three free helpers used by the database
serialization / import path; none of them depended on Flask, FAB or the
request context, so the port is mechanical.

* :func:`mask_password_info` strips secret-bearing fields out of an
  ``ssh_tunnel`` payload (used when a database is exported / shown to
  the user).
* :func:`unmask_password_info` is the inverse — it patches a payload
  coming from the API back from the persisted model when the secret
  fields still hold the ``PASSWORD_MASK`` sentinel.
* :func:`get_default_port` returns the well-known port for the given
  database backend.

The actual SSH-forwarder construction (``sshtunnel.SSHTunnelForwarder``)
is performed inside the engine-spec layer (``db_engine_specs``) and is
not part of this module — this matches the original layout.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.constants import PASSWORD_MASK

if TYPE_CHECKING:  # pragma: no cover - typing only
    from superset.models.ssh_tunnel import SSHTunnel


# Default ports per database backend (verbatim from the original).
DEFAULT_PORTS: dict[str, int] = {
    "postgresql": 5432,
    "mysql": 3306,
    "oracle": 1521,
    "mssql": 1433,
}


def mask_password_info(ssh_tunnel: dict[str, Any]) -> dict[str, Any]:
    """Replace secret-bearing fields with the ``PASSWORD_MASK`` sentinel.

    Mirrors ``superset_old.utils.ssh_tunnel.mask_password_info``: any of
    ``password``, ``private_key`` or ``private_key_password`` that are
    present and non-``None`` in the incoming dict are popped and
    replaced with ``PASSWORD_MASK`` so the masked value is what callers
    serialize back to the user.
    """
    if ssh_tunnel.pop("password", None) is not None:
        ssh_tunnel["password"] = PASSWORD_MASK
    if ssh_tunnel.pop("private_key", None) is not None:
        ssh_tunnel["private_key"] = PASSWORD_MASK
    if ssh_tunnel.pop("private_key_password", None) is not None:
        ssh_tunnel["private_key_password"] = PASSWORD_MASK
    return ssh_tunnel


def unmask_password_info(
    ssh_tunnel: dict[str, Any], model: SSHTunnel
) -> dict[str, Any]:
    """Patch masked values back from the persisted ``SSHTunnel`` model.

    Mirrors ``superset_old.utils.ssh_tunnel.unmask_password_info``: when
    the inbound payload contains the ``PASSWORD_MASK`` sentinel for
    ``password``, ``private_key`` or ``private_key_password``, the value
    is replaced with the corresponding attribute from ``model``.
    """
    if ssh_tunnel.get("password") == PASSWORD_MASK:
        ssh_tunnel["password"] = model.password
    if ssh_tunnel.get("private_key") == PASSWORD_MASK:
        ssh_tunnel["private_key"] = model.private_key
    if ssh_tunnel.get("private_key_password") == PASSWORD_MASK:
        ssh_tunnel["private_key_password"] = model.private_key_password
    return ssh_tunnel


def get_default_port(backend: str) -> int | None:
    """Return the default port for the given backend, or ``None``.

    Verbatim from ``superset_old.utils.ssh_tunnel.get_default_port``.
    """
    return DEFAULT_PORTS.get(backend)
