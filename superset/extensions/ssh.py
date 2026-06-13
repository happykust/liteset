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
"""1:1 port of ``superset_old/extensions/ssh.py``.

Hosts the canonical ``SSHManager`` and ``SSHManagerFactory`` referenced by
the default ``SSH_TUNNEL_MANAGER_CLASS`` setting.  The application
parameter is replaced by a generic ``settings`` object so the manager can
be used from both the Litestar app-startup path and from Celery tasks.
"""

from __future__ import annotations

import logging
from io import StringIO
from typing import Any, cast, TYPE_CHECKING

import sshtunnel
from paramiko import RSAKey

from superset.databases.utils import make_url_safe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from superset.models.ssh_tunnel import SSHTunnel


logger = logging.getLogger(__name__)


def _resolve_setting(source: Any, *names: str, default: Any = None) -> Any:
    """Read a config knob from *source*.

    Accepts both Litestar Pydantic settings (snake_case attributes) and
    legacy upstream-style mapping objects (``UPPER_CASE`` dict keys) so
    the same factory can be reused across the entire codebase.
    """
    for name in names:
        if hasattr(source, name):
            return getattr(source, name)
        if hasattr(source, "get"):
            value = source.get(name)
            if value is not None:
                return value
    return default


class SSHManager:
    """Build SSH tunnels for downstream database engines.

    Direct port of :class:`superset_old.extensions.ssh.SSHManager`.  The
    only behavioural change is that ``app`` is generalised to any object
    exposing the SSH-tunnel knobs — the Litestar settings model, a
    plain ``dict`` (upstream-style ``app.config``), or a Celery worker's
    ``current_app.conf``.
    """

    def __init__(self, app: Any) -> None:
        super().__init__()
        self.local_bind_address: str = _resolve_setting(
            app,
            "ssh_tunnel_local_bind_address",
            "SSH_TUNNEL_LOCAL_BIND_ADDRESS",
            default="127.0.0.1",
        )
        sshtunnel.TUNNEL_TIMEOUT = _resolve_setting(
            app,
            "ssh_tunnel_timeout_sec",
            "SSH_TUNNEL_TIMEOUT_SEC",
            default=10.0,
        )
        sshtunnel.SSH_TIMEOUT = _resolve_setting(
            app,
            "ssh_tunnel_packet_timeout_sec",
            "SSH_TUNNEL_PACKET_TIMEOUT_SEC",
            default=1.0,
        )

    def build_sqla_url(
        self,
        sqlalchemy_url: str,
        server: sshtunnel.SSHTunnelForwarder,
    ) -> Any:
        """Rewrite *sqlalchemy_url* to point at the local tunnel endpoint.

        Mirrors the original — the inbound URL is parsed, ``host`` and
        ``port`` are replaced with ``server.local_bind_address`` /
        ``server.local_bind_port``, and the resulting :class:`URL` is
        returned (callers stringify as needed).
        """
        url = make_url_safe(sqlalchemy_url)
        return url.set(
            host=server.local_bind_address[0],
            port=server.local_bind_port,
        )

    def create_tunnel(
        self,
        ssh_tunnel: "SSHTunnel",
        sqlalchemy_database_uri: str,
    ) -> sshtunnel.SSHTunnelForwarder:
        """Open an :class:`SSHTunnelForwarder` for *ssh_tunnel*.

        Verbatim port — the only adjustment is that
        :func:`get_default_port` now lives in
        :mod:`superset.utils.ssh_tunnel` (already true in the original).
        """
        from superset.utils.ssh_tunnel import get_default_port

        url = make_url_safe(sqlalchemy_database_uri)
        backend = url.get_backend_name()
        params: dict[str, Any] = {
            "ssh_address_or_host": (
                ssh_tunnel.server_address,
                ssh_tunnel.server_port,
            ),
            "ssh_username": ssh_tunnel.username,
            "remote_bind_address": (
                url.host,
                url.port or get_default_port(backend),
            ),
            "local_bind_address": (self.local_bind_address,),
            "debug_level": logging.getLogger("superset").level,
        }

        if ssh_tunnel.password:
            params["ssh_password"] = ssh_tunnel.password
        elif ssh_tunnel.private_key:
            private_key_file = StringIO(cast("str", ssh_tunnel.private_key))
            private_key = RSAKey.from_private_key(
                private_key_file,
                cast("str | None", ssh_tunnel.private_key_password),
            )
            params["ssh_pkey"] = private_key

        return sshtunnel.open_tunnel(**params)


class SSHManagerFactory:
    """Lazy factory mirroring :class:`SSHManagerFactory` from the original.

    ``init_app`` resolves :setting:`SSH_TUNNEL_MANAGER_CLASS` (a dotted
    path) to a concrete class and instantiates it with the supplied
    application / settings object.  ``instance`` returns the cached
    manager — the property is accessed lazily because some Liteset call
    sites (e.g. database serialisation) may run before ``init_app`` has
    been invoked when no SSH tunnel is configured.
    """

    def __init__(self) -> None:
        self._ssh_manager: SSHManager | None = None

    def init_app(self, app: Any) -> None:
        from superset.utils.class_utils import load_class_from_name

        manager_class_path = _resolve_setting(
            app,
            "ssh_tunnel_manager_class",
            "SSH_TUNNEL_MANAGER_CLASS",
            default="superset.extensions.ssh.SSHManager",
        )
        manager_cls = load_class_from_name(manager_class_path)
        self._ssh_manager = manager_cls(app)

    @property
    def instance(self) -> SSHManager:
        if self._ssh_manager is None:
            raise RuntimeError(
                "SSHManagerFactory.init_app(app) must be called before "
                "accessing SSHManagerFactory.instance"
            )
        return self._ssh_manager


# Module-level singleton — mirrors the original
# ``superset.extensions.ssh_manager_factory`` symbol that downstream code
# imported.  Initialised lazily by :func:`superset.app.on_startup` (and
# rebound for tests via the same factory).
ssh_manager_factory = SSHManagerFactory()


__all__ = ["SSHManager", "SSHManagerFactory", "ssh_manager_factory"]
