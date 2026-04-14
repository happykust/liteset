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
"""Network helpers — ported 1:1 from ``superset_old/utils/network.py``."""

from __future__ import annotations

import platform
import socket
import subprocess
from typing import Any

PORT_TIMEOUT = 5
PING_TIMEOUT = 5


def is_port_open(host: str, port: int) -> bool:
    """
    Test if a given port in a host is open.
    """
    # pylint: disable=invalid-name
    for res in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        af, _, _, _, sockaddr = res
        s = socket.socket(af, socket.SOCK_STREAM)
        try:
            s.settimeout(PORT_TIMEOUT)
            s.connect(sockaddr)
            s.shutdown(socket.SHUT_RDWR)
            return True
        except OSError:
            continue
        finally:
            s.close()
    return False


_DNS_RESOLVE_TIMEOUT = 1.0
_dns_executor: Any = None


def _get_dns_executor() -> Any:
    """Lazy shared thread pool so abandoned DNS lookups don't block shutdown."""
    global _dns_executor  # noqa: PLW0603
    if _dns_executor is None:
        import concurrent.futures

        _dns_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="superset-dns",
        )
    return _dns_executor


def is_hostname_valid(host: str) -> bool:
    """Test if a given hostname can be resolved.

    ``socket.getaddrinfo`` blocks for the full libc resolver retry
    chain (~4 s default on Linux) when the hostname is unresolvable.
    In the original sync Flask backend that was tolerable because each
    request was handled by its own worker thread, but in the async
    Litestar port the cumulative delay across sequential
    ``validate_parameters`` requests (one per form-field blur) pushes
    the ``database/modal`` Cypress test past its 8 s retry budget for
    the ``Connect`` button-enable assertion.

    ``getaddrinfo`` does not honour ``socket.setdefaulttimeout``, so
    we enforce an upper bound by running the lookup in a shared worker
    thread pool and treating any timeout as ``False`` (unresolvable).
    The shared pool is important: a per-call context manager would
    block on executor shutdown until the abandoned resolver thread
    completes, which defeats the timeout.
    """
    import concurrent.futures

    def _resolve() -> bool:
        try:
            socket.getaddrinfo(host, None)
            return True
        except socket.gaierror:
            return False

    future = _get_dns_executor().submit(_resolve)
    try:
        return future.result(timeout=_DNS_RESOLVE_TIMEOUT)
    except concurrent.futures.TimeoutError:
        # Don't block on the abandoned lookup; just cancel best-effort.
        future.cancel()
        return False


def is_host_up(host: str) -> bool:
    """
    Ping a host to see if it's up.

    Note that if we don't get a response the host might still be up,
    since many firewalls block ICMP packets.
    """
    param = "-n" if platform.system().lower() == "windows" else "-c"
    command = ["ping", param, "1", host]
    try:
        output = subprocess.call(command, timeout=PING_TIMEOUT)  # noqa: S603
    except subprocess.TimeoutExpired:
        return False

    return output == 0


__all__ = ["is_host_up", "is_hostname_valid", "is_port_open"]
