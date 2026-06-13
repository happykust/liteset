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
"""Pluggable authentication backends.

Each module in this sub-package implements a single ``AUTH_TYPE``
value as supported by Apache Superset upstream:

==========================================  =================  ==================
File                                        ``AUTH_TYPE`` int  Original hook
==========================================  =================  ==================
:mod:`superset.security.auth.oauth`         ``4`` (AUTH_OAUTH) ``auth_user_oauth``
:mod:`superset.security.auth.oidc`          ``4`` (AUTH_OAUTH) ``auth_user_oauth``
:mod:`superset.security.auth.remote_user`   ``3`` (AUTH_REMOTE_USER)
                                                               ``auth_user_remote_user``
==========================================  =================  ==================

OIDC re-uses the OAuth flow with Authlib's discovery-document support,
which is exactly how Apache Superset (since the 4.x security layer)
handles OpenID Connect — there is no separate ``auth_user_oid`` hook
in the modern upstream.
"""

from __future__ import annotations

from superset.security.auth.oauth import (
    OAuthAuthBackend,
    OAuthCallbackError,
    OAuthProviderUnknown,
)
from superset.security.auth.oidc import OIDCAuthBackend
from superset.security.auth.remote_user import RemoteUserAuthBackend

__all__ = [
    "OAuthAuthBackend",
    "OAuthCallbackError",
    "OAuthProviderUnknown",
    "OIDCAuthBackend",
    "RemoteUserAuthBackend",
]
