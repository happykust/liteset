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

"""SSH tunnel helpers — ported from ``superset_old/utils/ssh_tunnel.py``
and ``superset_old/extensions/ssh.py`` to the Liteset runtime.

Free helpers (mask/unmask passwords, port lookup) plus the ``SSHManager``
and ``SSHManagerFactory`` classes that the original ``extensions/ssh.py``
provided.  Flask has been removed; settings are accepted via constructor
kwargs so the classes work in both the ASGI runtime and CLI tools.

* :func:`mask_password_info` — strips secret fields from payloads.
* :func:`unmask_password_info` — restores masked fields from the DB model.
* :func:`get_default_port` — well-known port for a backend name.
* :class:`SSHManager` — builds SQLAlchemy URLs and opens tunnels.
* :class:`SSHManagerFactory` — process-wide singleton that holds the
  configured :class:`SSHManager` instance.
"""

from __future__ import annotations

import logging
from io import StringIO
from typing import Any, cast, TYPE_CHECKING

from superset.constants import PASSWORD_MASK

if TYPE_CHECKING:  # pragma: no cover - typing only
    from superset.models.ssh_tunnel import SSHTunnel

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# SSHManager / SSHManagerFactory
# Ported from ``superset_old/extensions/ssh.py``.
# Flask dependency removed — settings are passed as constructor kwargs.
# ---------------------------------------------------------------------------


class SSHManager:
    """Opens SSH tunnels and rewrites SQLAlchemy URLs to use the tunnel.

    Ported 1:1 from ``superset_old/extensions/ssh.SSHManager`` with the
    Flask ``app.config`` dependency replaced by explicit constructor
    parameters that match the ``SupersetSettings`` field names.
    """

    def __init__(
        self,
        local_bind_address: str = "127.0.0.1",
        tunnel_timeout: float = 10.0,
        packet_timeout: float = 1.0,
    ) -> None:
        import sshtunnel

        self.local_bind_address = local_bind_address
        sshtunnel.TUNNEL_TIMEOUT = tunnel_timeout
        sshtunnel.SSH_TIMEOUT = packet_timeout

    # ------------------------------------------------------------------
    # Alternative constructor — accepts a SupersetSettings instance so
    # the factory can call ``SSHManager(settings)`` without changes to
    # downstream call sites.
    # ------------------------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Any) -> "SSHManager":
        """Construct from a :class:`~superset.config.SupersetSettings` object."""
        return cls(
            local_bind_address=getattr(
                settings, "ssh_tunnel_local_bind_address", "127.0.0.1"
            ),
            tunnel_timeout=getattr(settings, "ssh_tunnel_timeout_sec", 10.0),
            packet_timeout=getattr(settings, "ssh_tunnel_packet_timeout_sec", 1.0),
        )

    def build_sqla_url(
        self,
        sqlalchemy_url: str,
        server: Any,  # sshtunnel.SSHTunnelForwarder
    ) -> str:
        """Rewrite *sqlalchemy_url* to route through the SSH tunnel."""
        from sqlalchemy.engine import make_url

        url = make_url(sqlalchemy_url)
        return str(
            url.set(
                host=server.local_bind_address[0],
                port=server.local_bind_port,
            )
        )

    def create_tunnel(
        self,
        ssh_tunnel: "SSHTunnel",
        sqlalchemy_database_uri: str,
    ) -> Any:  # sshtunnel.SSHTunnelForwarder
        """Open an SSH tunnel for *sqlalchemy_database_uri*.

        Mirrors the original ``SSHManager.create_tunnel`` 1:1:
        prefers ``password`` auth, falls back to RSA private key.
        """
        import sshtunnel
        from paramiko import RSAKey
        from sqlalchemy.engine import make_url

        url = make_url(sqlalchemy_database_uri)
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
            "debug_level": logging.getLogger(__name__).level,
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
    """Process-wide factory / holder for the configured :class:`SSHManager`.

    Matches the original ``superset_old/extensions/ssh.SSHManagerFactory``
    interface except that ``init_app`` now accepts a
    :class:`~superset.config.SupersetSettings` object (or any object with
    the ``ssh_tunnel_*`` attributes) rather than a Flask ``app``.
    """

    def __init__(self) -> None:
        self._ssh_manager: SSHManager | None = None

    def init_app(self, settings: Any) -> None:
        """Instantiate the configured SSH manager class from settings.

        The manager class is resolved via ``settings.ssh_tunnel_manager_class``
        (a dotted-path string).  If the class accepts a ``settings`` kwarg it
        is called with it; otherwise it is called with positional equivalents
        mirroring the Flask-config-based original.
        """
        from superset.utils.class_utils import load_class_from_name

        manager_cls_path: str = getattr(
            settings,
            "ssh_tunnel_manager_class",
            "superset.utils.ssh_tunnel.SSHManager",
        )
        manager_cls = load_class_from_name(manager_cls_path)

        # Prefer the ``from_settings`` classmethod when present (our own
        # SSHManager provides it); fall back to the legacy positional
        # constructor used by the old Flask-based class.
        if hasattr(manager_cls, "from_settings"):
            self._ssh_manager = manager_cls.from_settings(settings)
        else:
            self._ssh_manager = manager_cls(
                local_bind_address=getattr(
                    settings, "ssh_tunnel_local_bind_address", "127.0.0.1"
                ),
                tunnel_timeout=getattr(settings, "ssh_tunnel_timeout_sec", 10.0),
                packet_timeout=getattr(settings, "ssh_tunnel_packet_timeout_sec", 1.0),
            )

    @property
    def instance(self) -> SSHManager:
        if self._ssh_manager is None:
            raise RuntimeError(
                "SSHManagerFactory.init_app() has not been called yet. "
                "Ensure on_startup has run before accessing "
                "ssh_manager_factory.instance."
            )
        return self._ssh_manager
