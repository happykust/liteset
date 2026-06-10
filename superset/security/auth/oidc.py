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
"""OpenID Connect authentication backend.

OIDC re-uses the OAuth Authorization Code flow with these additions:

1. The provider exposes a *discovery document* at
   ``server_metadata_url`` that pins the authorize / token / userinfo
   endpoints and the JWKS endpoint.
2. The token endpoint returns an ``id_token`` JWT alongside the access
   token.  The ID token is signed by the IdP and contains the user's
   identity claims (``sub``, ``email``, ``preferred_username``, etc.).
3. Liteset validates the ``id_token`` signature against the provider's
   JWKS and uses the resulting claims as the canonical user-info
   payload.

This module is the async equivalent of FAB's
:pymeth:`BaseSecurityManager._validate_jwt`,
:pymeth:`BaseSecurityManager._decode_and_validate_azure_jwt`,
and the per-provider OIDC branches inside
:pymeth:`BaseSecurityManager.get_oauth_user_info`.

Modern Apache Superset / FAB no longer exposes a separate ``AUTH_OID``
type — every OIDC provider is configured via ``AUTH_TYPE = AUTH_OAUTH``
plus a ``server_metadata_url``.  This backend exists so call sites that
explicitly want OIDC validation can reach a typed entry point; the OAuth
backend itself falls back to OIDC discovery automatically.
"""

from __future__ import annotations

import logging
from typing import Any

import jwt as pyjwt

from superset.security.auth.oauth import OAuthAuthBackend, OAuthCallbackError

logger = logging.getLogger(__name__)


class OIDCAuthBackend(OAuthAuthBackend):
    """OAuth backend that performs full OIDC ``id_token`` validation.

    Unlike :class:`OAuthAuthBackend` which decodes the ID token without
    signature verification (matching FAB's default behaviour), this
    backend always validates the ``id_token`` against the provider's
    JWKS and returns the validated claims as the user-info dict.
    """

    async def get_user_info(
        self,
        *,
        provider_name: str,
        provider: dict[str, Any],
        token_resp: dict[str, Any],
        endpoints: dict[str, str],
    ) -> dict[str, Any]:
        """Return validated user-info claims for an OIDC flow."""

        id_token: str = token_resp.get("id_token", "")
        access_token: str = token_resp.get("access_token", "")

        # Prefer the id_token when present — it's already signed by the
        # IdP and contains the canonical user identity.
        if id_token:
            try:
                claims = await self.validate_id_token(
                    id_token=id_token,
                    jwks_uri=endpoints["jwks_uri"],
                    issuer=endpoints["issuer"],
                    audience=str(provider.get("client_id") or ""),
                )
            except OAuthCallbackError as exc:
                logger.error("OIDC id_token validation failed: %s", exc)
                claims = {}

            if claims:
                return self._claims_to_userinfo(claims)

        # Fall back to UserInfo endpoint when the IdP is OAuth-only or
        # when the id_token didn't validate.
        userinfo_url: str = endpoints["userinfo_url"]
        if userinfo_url and access_token:
            data = await self._http_get_json(userinfo_url, bearer=access_token)
            if data:
                return self._claims_to_userinfo(data)

        return {}

    async def validate_id_token(
        self,
        *,
        id_token: str,
        jwks_uri: str,
        issuer: str = "",
        audience: str = "",
    ) -> dict[str, Any]:
        """Validate an OIDC ``id_token`` against the provider's JWKS.

        Uses :mod:`PyJWT`'s :class:`PyJWKClient` to fetch and cache the
        JWKS.  Mirrors :pymeth:`BaseSecurityManager._validate_jwt`
        (FAB ``manager.py:794-801``) which does the same with authlib.
        """
        if not id_token or not jwks_uri:
            raise OAuthCallbackError("OIDC validation requires id_token and jwks_uri")

        # PyJWKClient is sync but the network calls inside it are short
        # and cached after first hit.  We dispatch them via ``to_thread``
        # so the event loop never blocks.
        import asyncio

        def _validate() -> dict[str, Any]:
            jwk_client = pyjwt.PyJWKClient(jwks_uri)
            signing_key = jwk_client.get_signing_key_from_jwt(id_token).key

            # PyJWT requires the algorithm list — extract from the JWT
            # header so we match whatever the IdP signed with.
            unverified_header = pyjwt.get_unverified_header(id_token)
            algorithms = [unverified_header.get("alg", "RS256")]

            options: dict[str, bool] = {"verify_signature": True}
            decode_kwargs: dict[str, Any] = {
                "algorithms": algorithms,
                "options": options,
            }
            if audience:
                decode_kwargs["audience"] = audience
            else:
                # PyJWT requires verify_aud=False when no audience is given
                options["verify_aud"] = False
            if issuer:
                decode_kwargs["issuer"] = issuer

            return pyjwt.decode(id_token, signing_key, **decode_kwargs)

        try:
            return await asyncio.to_thread(_validate)
        except pyjwt.PyJWTError as exc:
            raise OAuthCallbackError(f"OIDC id_token validation failed: {exc}") from exc

    @staticmethod
    def _claims_to_userinfo(claims: dict[str, Any]) -> dict[str, Any]:
        """Translate OIDC claims to FAB's ``userinfo`` shape.

        Output keys match :pymeth:`AsyncSecurityManager.auth_user_oauth`'s
        expected input: ``username``, ``first_name``, ``last_name``,
        ``email``, ``role_keys``.
        """
        return {
            "username": claims.get("preferred_username", "")
            or claims.get("nickname", "")
            or claims.get("sub", ""),
            "first_name": claims.get("given_name", ""),
            "last_name": claims.get("family_name", ""),
            "email": claims.get("email", ""),
            "role_keys": claims.get("groups", []) or claims.get("roles", []) or [],
        }
