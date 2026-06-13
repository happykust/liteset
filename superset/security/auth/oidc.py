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

from superset.security.auth.oauth import (
    _provider_remote_app,
    OAuthAuthBackend,
    OAuthCallbackError,
)

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
        expected_nonce: str = "",
    ) -> dict[str, Any]:
        """Return validated user-info claims for an OIDC flow."""

        id_token: str = token_resp.get("id_token", "")
        access_token: str = token_resp.get("access_token", "")

        # Prefer the id_token when present — it's already signed by the
        # IdP and contains the canonical user identity.
        if id_token:
            # Resolve ``client_id`` through ``_provider_remote_app`` so the
            # audience is enforced regardless of whether the provider uses
            # the top-level or ``remote_app`` config layout.  Reading it
            # straight off the raw ``provider`` dict yields ``None`` for the
            # ``remote_app`` layout, which would set ``verify_aud=False`` and
            # accept an id_token minted for a different relying party.
            remote = _provider_remote_app(provider)
            audience = str(remote.get("client_id") or "")
            # Do NOT swallow validation failures: a bad signature or wrong
            # audience must reject the login, not silently fall through to
            # the UserInfo endpoint (which masks the rejection). Mirrors FAB
            # ``_get_authentik_token_info`` raising ``InvalidLoginAttempt``
            # on a failed signature verification.
            claims = await self.validate_id_token(
                id_token=id_token,
                jwks_uri=endpoints["jwks_uri"],
                issuer=endpoints["issuer"],
                audience=audience,
                nonce=expected_nonce,
            )

            if claims:
                # Authentik uses an idiosyncratic claim mapping (FAB
                # ``get_oauth_user_info`` authentik branch): ``email`` comes
                # from ``preferred_username`` and ``username`` from
                # ``nickname`` — distinct from the standard OIDC names the
                # generic translator applies. The id_token is already
                # signature-validated above.
                if provider_name == "authentik":
                    return {
                        "email": claims.get("preferred_username", ""),
                        "first_name": claims.get("given_name", ""),
                        "last_name": claims.get("family_name", ""),
                        "username": claims.get("nickname", ""),
                        "role_keys": claims.get("groups", []) or [],
                    }
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
        nonce: str = "",
    ) -> dict[str, Any]:
        """Validate an OIDC ``id_token`` against the provider's JWKS.

        Uses :mod:`PyJWT`'s :class:`PyJWKClient` to fetch and cache the
        JWKS.  Mirrors :pymeth:`BaseSecurityManager._validate_jwt`
        (FAB ``manager.py:794-801``) which does the same with authlib.

        When ``nonce`` is supplied the token's ``nonce`` claim must match it
        exactly (replay / token-injection protection); a mismatch rejects
        the login.
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

            claims = pyjwt.decode(id_token, signing_key, **decode_kwargs)

            if nonce:
                import hmac

                token_nonce = str(claims.get("nonce") or "")
                if not hmac.compare_digest(token_nonce, nonce):
                    raise pyjwt.InvalidTokenError(
                        "id_token nonce does not match the authorization request"
                    )
            return claims

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
