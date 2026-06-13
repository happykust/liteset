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
"""OAuth 2.0 / Authorization Code authentication backend.

Async port of Flask-AppBuilder's OAuth flow:

* :pymeth:`flask_appbuilder.security.manager.BaseSecurityManager.auth_user_oauth`
  (``manager.py:1469``)
* :pymeth:`flask_appbuilder.security.manager.BaseSecurityManager.get_oauth_user_info`
  (``manager.py:647``)
* :class:`flask_appbuilder.security.views.AuthOAuthView`
  (``views.py:662``)

The control flow is identical:

1. Read the configured ``OAUTH_PROVIDERS`` list from settings and pick a
   provider by name.
2. Build the authorize redirect URL and ship the user there.
3. On callback, exchange ``code`` for a token, then fetch the user-info
   document (or decode the ID token for OIDC providers).
4. Map the returned ``role_keys`` / group claims to local roles via
   ``AUTH_ROLES_MAPPING``.
5. Look up the user in ``ab_user`` (case-insensitive, mirrors FAB) and
   either return them, sync their roles, or self-register a new row when
   ``AUTH_USER_REGISTRATION`` is enabled.

Differences from FAB:

* Uses ``httpx.AsyncClient`` instead of ``authlib.integrations.flask_client``.
  The Authorization-Code → Access-Token exchange and the user-info GET
  are pure HTTP calls, so we do not need authlib's Flask integration.
  When the user configures ``server_metadata_url`` (OIDC discovery), we
  still pull the document via httpx and use the discovered endpoints.
* JWT validation for OIDC providers reuses :mod:`PyJWT` (already a
  dependency for guest tokens) — same algorithms, JWKS fetched via
  httpx.  Authlib remains an optional dep for callers that want richer
  validation (audience checking on multiple aud claims, etc.).
* The ``state`` parameter is signed with the application ``SECRET_KEY``
  (HS256) so stateful OAuth providers (Twitter et al.) keep their
  request-scoped data without leaning on Flask's ``session`` dict.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import jwt as pyjwt

logger = logging.getLogger(__name__)


class OAuthCallbackError(Exception):
    """Raised when the OAuth callback cannot be processed."""


class OAuthProviderUnknown(Exception):  # noqa: N818
    """Raised when no provider with the given name is configured."""


def _find_provider(providers: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Look up a provider entry from ``OAUTH_PROVIDERS`` by name."""
    for provider in providers:
        if provider.get("name") == name:
            return provider
    raise OAuthProviderUnknown(f"OAuth provider not configured: {name}")


def _provider_remote_app(provider: dict[str, Any]) -> dict[str, Any]:
    """Return the ``remote_app`` config dict for a provider entry.

    Apache Superset / FAB documents two equivalent layouts:
    - ``remote_app`` key holding the authlib config dict, or
    - top-level keys (``client_id``, ``client_secret``, …).

    We accept both for parity with existing ``superset_config.py`` files.
    """
    if "remote_app" in provider and isinstance(provider["remote_app"], dict):
        return provider["remote_app"]
    return provider


class OAuthAuthBackend:
    """Authorization-Code OAuth 2.0 backend.

    Bound to a single :class:`AsyncSecurityManager` instance.  Each
    incoming HTTP request that completes the OAuth dance constructs a
    fresh backend with the request-scoped SM (same object that handles
    DB-auth and LDAP-auth), so per-request state (``state`` cookie,
    ``next`` URL) flows through method arguments rather than ContextVars.
    """

    DEFAULT_HTTP_TIMEOUT: float = 10.0

    def __init__(self, security_manager: Any, *, settings: Any) -> None:
        self._sm = security_manager
        self._settings = settings

    # ------------------------------------------------------------------
    # Provider discovery / configuration
    # ------------------------------------------------------------------

    def get_providers(self) -> list[dict[str, Any]]:
        """Return the configured ``OAUTH_PROVIDERS`` list."""
        return getattr(self._settings, "oauth_providers", []) or []

    def get_provider(self, name: str) -> dict[str, Any]:
        """Look up a provider by name, raising on miss."""
        return _find_provider(self.get_providers(), name)

    async def _resolve_endpoints(self, provider: dict[str, Any]) -> dict[str, str]:
        """Resolve authorize / token / userinfo URLs.

        Uses ``server_metadata_url`` (OIDC discovery doc) when set,
        otherwise reads the explicit URL keys (FAB layout).
        """
        remote = _provider_remote_app(provider)
        endpoints: dict[str, str] = {
            "authorize_url": remote.get("authorize_url", "")
            or remote.get("api_base_url", "") + "authorize",  # noqa: E501
            "access_token_url": remote.get("access_token_url", ""),
            "userinfo_url": remote.get("userinfo_endpoint", "")
            or remote.get("userinfo_url", ""),
            "jwks_uri": remote.get("jwks_uri", ""),
            "issuer": remote.get("issuer", ""),
        }

        metadata_url: str = remote.get("server_metadata_url", "") or ""
        if metadata_url:
            metadata = await self._http_get_json(metadata_url)
            if metadata:
                endpoints["authorize_url"] = (
                    metadata.get("authorization_endpoint") or endpoints["authorize_url"]
                )
                endpoints["access_token_url"] = (
                    metadata.get("token_endpoint") or endpoints["access_token_url"]
                )
                endpoints["userinfo_url"] = (
                    metadata.get("userinfo_endpoint") or endpoints["userinfo_url"]
                )
                endpoints["jwks_uri"] = (
                    metadata.get("jwks_uri") or endpoints["jwks_uri"]
                )
                endpoints["issuer"] = metadata.get("issuer") or endpoints["issuer"]
        return endpoints

    # ------------------------------------------------------------------
    # State signing (replaces Flask session storage)
    # ------------------------------------------------------------------

    def _state_secret(self) -> str:
        """Resolve the application secret key as a plain string."""
        sk = getattr(self._settings, "secret_key", "") or ""
        if hasattr(sk, "get_secret_value"):
            return sk.get_secret_value()
        return str(sk)

    def sign_state(self, payload: dict[str, Any]) -> str:
        """Sign an OAuth ``state`` payload with HS256.

        Mirrors :class:`AuthOAuthView.login`'s
        ``jwt.encode(request.args.to_dict(flat=False), random_state, "HS256")``
        but uses the application ``SECRET_KEY`` so the same value can be
        verified from the callback handler without sharing in-memory
        session state.
        """
        nonce = secrets.token_urlsafe(16)
        body = dict(payload)
        body.setdefault("nonce", nonce)
        return pyjwt.encode(body, self._state_secret(), algorithm="HS256")

    def verify_state(self, token: str) -> dict[str, Any]:
        """Verify a previously-signed state token, returning the payload.

        Raises :class:`OAuthCallbackError` on signature/format failure.
        """
        try:
            return pyjwt.decode(token, self._state_secret(), algorithms=["HS256"])
        except pyjwt.PyJWTError as exc:
            raise OAuthCallbackError(f"Invalid OAuth state: {exc}") from exc

    # ------------------------------------------------------------------
    # Authorization request
    # ------------------------------------------------------------------

    async def build_authorize_url(
        self,
        provider_name: str,
        *,
        redirect_uri: str,
        next_url: str = "",
        scope: str | None = None,
    ) -> tuple[str, str]:
        """Build the redirect URL for the Authorization Endpoint.

        Mirrors :pymeth:`AuthOAuthView.login` (``views.py:667-707``).

        Returns ``(redirect_url, signed_state)``.  Callers are responsible
        for persisting the signed state in a cookie keyed
        ``superset_oauth_state`` so the callback can verify it.
        """
        provider = self.get_provider(provider_name)
        endpoints = await self._resolve_endpoints(provider)
        if not endpoints["authorize_url"]:
            raise OAuthCallbackError(
                f"Provider '{provider_name}' has no authorize_url configured"
            )
        remote = _provider_remote_app(provider)
        if not remote.get("client_id"):
            raise OAuthCallbackError(
                f"Provider '{provider_name}' has no client_id configured"
            )

        scope = (
            scope
            or remote.get("client_kwargs", {}).get("scope")
            or "openid email profile"
        )  # noqa: E501

        # OIDC nonce — binds the issued id_token to this authorization
        # request (replay / token-injection defense, mirrors what FAB's
        # authlib client sets automatically). Carried inside the signed,
        # tamper-proof state so the callback can compare it to the
        # id_token's ``nonce`` claim without a separate cookie.
        nonce = secrets.token_urlsafe(32)
        state = self.sign_state(
            {"provider": provider_name, "next": next_url, "nonce": nonce}
        )

        params = {
            "client_id": remote["client_id"],
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "nonce": nonce,
        }
        delim = "&" if "?" in endpoints["authorize_url"] else "?"
        return f"{endpoints['authorize_url']}{delim}{urlencode(params)}", state

    # ------------------------------------------------------------------
    # Callback handling
    # ------------------------------------------------------------------

    async def handle_callback(
        self,
        provider_name: str,
        *,
        code: str,
        state: str,
        signed_state_cookie: str,
        redirect_uri: str,
    ) -> tuple[Any | None, str]:
        """Complete the Authorization Code grant.

        Verifies that ``state`` came from this server, exchanges ``code``
        for an access token (and ``id_token`` when present), pulls the
        user-info document, and authenticates / registers the user via
        :pymeth:`AsyncSecurityManager.auth_user_oauth`.

        Returns ``(user, next_url)``.  ``user`` is ``None`` when
        authentication fails.

        Mirrors :pymeth:`AuthOAuthView.oauth_authorized`
        (``views.py:709-767``).
        """
        if state != signed_state_cookie:
            raise OAuthCallbackError("OAuth state mismatch")
        payload = self.verify_state(state)
        if payload.get("provider") != provider_name:
            raise OAuthCallbackError("OAuth state provider mismatch")
        next_url: str = str(payload.get("next") or "") or ""
        expected_nonce: str = str(payload.get("nonce") or "")

        provider = self.get_provider(provider_name)
        remote = _provider_remote_app(provider)
        endpoints = await self._resolve_endpoints(provider)

        if not endpoints["access_token_url"]:
            raise OAuthCallbackError(
                f"Provider '{provider_name}' has no token endpoint"
            )

        token_resp = await self._exchange_code_for_token(
            token_url=endpoints["access_token_url"],
            client_id=remote["client_id"],
            client_secret=remote.get("client_secret", ""),
            code=code,
            redirect_uri=redirect_uri,
        )

        userinfo = await self.get_user_info(
            provider_name=provider_name,
            provider=provider,
            token_resp=token_resp,
            endpoints=endpoints,
            expected_nonce=expected_nonce,
        )

        # Apply email whitelist (mirrors FAB views.py:736-747).
        whitelist = remote.get("whitelist") or provider.get("whitelist")
        if whitelist:
            email = (userinfo or {}).get("email", "")
            import re

            if not any(re.search(pattern, email) for pattern in whitelist):
                logger.info("OAuth login denied: email '%s' not in whitelist", email)
                return None, next_url

        if not userinfo:
            return None, next_url

        user = await self._sm.auth_user_oauth(userinfo, settings=self._settings)
        return user, next_url

    # ------------------------------------------------------------------
    # User-info retrieval
    # ------------------------------------------------------------------

    async def get_user_info(  # noqa: C901
        self,
        *,
        provider_name: str,
        provider: dict[str, Any],
        token_resp: dict[str, Any],
        endpoints: dict[str, str],
        expected_nonce: str = "",
    ) -> dict[str, Any]:
        """Pull the user profile from the IdP.

        ``expected_nonce`` is accepted for signature parity with
        :class:`OIDCAuthBackend`; this base backend decodes id_tokens
        without signature verification, so a nonce comparison on an
        unverified token would add no security and is intentionally not
        performed here. Providers that need replay protection are routed
        to :class:`OIDCAuthBackend`, which validates the signature first.

        Ports the per-provider branches in
        :pymeth:`BaseSecurityManager.get_oauth_user_info` (``manager.py:647``)
        and returns a normalised dict with the same key set
        (``username``, ``first_name``, ``last_name``, ``email``,
        ``role_keys``).  Unknown providers fall back to the OIDC
        discovery doc's ``userinfo_endpoint`` and apply the standard
        OpenID claim names.
        """
        access_token: str = token_resp.get("access_token", "")
        id_token: str = token_resp.get("id_token", "")
        userinfo_url: str = endpoints["userinfo_url"]

        # GitHub
        if provider_name in ("github", "githublocal"):
            data = await self._http_get_json(
                userinfo_url or "https://api.github.com/user",
                bearer=access_token,
            )
            return {"username": "github_" + str(data.get("login", ""))}

        # Twitter (1.1)
        if provider_name == "twitter":
            data = await self._http_get_json(
                userinfo_url or "https://api.twitter.com/1.1/account/settings.json",
                bearer=access_token,
            )
            return {"username": "twitter_" + str(data.get("screen_name", ""))}

        # LinkedIn
        if provider_name == "linkedin":
            data = await self._http_get_json(
                userinfo_url
                or "https://api.linkedin.com/v2/me?projection=(id,localizedFirstName,localizedLastName)",
                bearer=access_token,
            )
            return {
                "username": "linkedin_" + str(data.get("id", "")),
                "email": data.get("emailAddress", ""),
                "first_name": data.get("localizedFirstName", ""),
                "last_name": data.get("localizedLastName", ""),
            }

        # Google
        if provider_name == "google":
            data = await self._http_get_json(
                userinfo_url or "https://openidconnect.googleapis.com/v1/userinfo",
                bearer=access_token,
            )
            return {
                "username": "google_" + str(data.get("sub") or data.get("id", "")),
                "first_name": data.get("given_name", ""),
                "last_name": data.get("family_name", ""),
                "email": data.get("email", ""),
            }

        # Azure AD — decode the id_token (mirrors FAB azure branch
        # ``_decode_and_validate_azure_jwt``). When the provider config sets
        # ``client_kwargs.verify_signature`` (default False, as in FAB) the
        # token signature is validated against Microsoft's static JWKS;
        # otherwise it is decoded unverified.
        if provider_name == "azure":
            remote = _provider_remote_app(provider)
            verify_signature = bool(
                remote.get("client_kwargs", {}).get("verify_signature", False)
            )
            if verify_signature:
                claims = await self._validate_azure_jwt(id_token)
            else:
                claims = self._decode_id_token_unsafe(id_token)
            return {
                "email": claims.get("upn", "") or claims.get("email", ""),
                "first_name": claims.get("given_name", ""),
                "last_name": claims.get("family_name", ""),
                "username": claims.get("oid", "") or claims.get("sub", ""),
                "role_keys": claims.get("roles", []) or [],
            }

        # Okta
        if provider_name == "okta":
            data = await self._http_get_json(userinfo_url, bearer=access_token)
            if "error" in data:
                logger.error(
                    "OAuth (okta) userinfo error: %s",
                    data.get("error_description"),
                )
                return {}
            return {
                # ``.get`` rather than ``data['sub']`` — a userinfo response
                # missing ``sub`` must not raise a KeyError (HTTP 500); an
                # empty username is rejected downstream by auth_user_oauth.
                "username": f"okta_{data.get('sub', '')}",
                "first_name": data.get("given_name", ""),
                "last_name": data.get("family_name", ""),
                "email": data.get("email", ""),
                "role_keys": data.get("groups", []) or [],
            }

        # Auth0
        if provider_name == "auth0":
            data = await self._http_get_json(userinfo_url, bearer=access_token)
            return {
                "username": f"auth0_{data.get('sub', '')}",
                "first_name": data.get("given_name", ""),
                "last_name": data.get("family_name", ""),
                "email": data.get("email", ""),
                "role_keys": data.get("groups", []) or [],
            }

        # Keycloak
        if provider_name in ("keycloak", "keycloak_before_17"):
            data = await self._http_get_json(userinfo_url, bearer=access_token)
            return {
                "username": data.get("preferred_username", ""),
                "first_name": data.get("given_name", ""),
                "last_name": data.get("family_name", ""),
                "email": data.get("email", ""),
                "role_keys": data.get("groups", []) or [],
            }

        # OpenShift — no OIDC discovery; identity comes from the cluster's
        # custom user API (mirrors FAB ``get_oauth_user_info`` openshift
        # branch: ``GET apis/user.openshift.io/v1/users/~``).
        if provider_name == "openshift":
            remote = _provider_remote_app(provider)
            api_base = str(remote.get("api_base_url", "")).rstrip("/")
            data = await self._http_get_json(
                f"{api_base}/apis/user.openshift.io/v1/users/~",
                bearer=access_token,
            )
            metadata = data.get("metadata") or {}
            return {"username": "openshift_" + str(metadata.get("name", ""))}

        # Authentik — identity from the id_token claims with Authentik's
        # idiosyncratic mapping (mirrors FAB ``get_oauth_user_info``
        # authentik branch: ``email = preferred_username``, ``username =
        # nickname``). This base backend decodes WITHOUT signature
        # verification (it's the no-discovery path); deployments that
        # configure a ``server_metadata_url`` / ``jwks_uri`` are routed to
        # :class:`OIDCAuthBackend`, which validates the id_token first.
        if provider_name == "authentik":
            claims = self._decode_id_token_unsafe(id_token)
            return {
                "email": claims.get("preferred_username", ""),
                "first_name": claims.get("given_name", ""),
                "last_name": claims.get("family_name", ""),
                "username": claims.get("nickname", ""),
                "role_keys": claims.get("groups", []) or [],
            }

        # Generic OIDC fallback — matches the standard
        # OpenID Connect claim names, used by any custom provider with a
        # discovery document.
        if userinfo_url and access_token:
            data = await self._http_get_json(userinfo_url, bearer=access_token)
            if data:
                return {
                    "email": data.get("email", "")
                    or data.get("preferred_username", ""),
                    "first_name": data.get("given_name", ""),
                    "last_name": data.get("family_name", ""),
                    "username": data.get("preferred_username", "")
                    or data.get("sub", "")
                    or data.get("nickname", ""),
                    "role_keys": data.get("groups", []) or [],
                }

        # Fall back to ID-token claims (last resort).
        if id_token:
            claims = self._decode_id_token_unsafe(id_token)
            return {
                "email": claims.get("email", "")
                or claims.get("preferred_username", ""),
                "first_name": claims.get("given_name", ""),
                "last_name": claims.get("family_name", ""),
                "username": claims.get("preferred_username", "")
                or claims.get("sub", ""),
                "role_keys": claims.get("groups", []) or [],
            }

        raise OAuthProviderUnknown(
            f"OAuth provider '{provider_name}' has no usable userinfo flow"
        )

    # ------------------------------------------------------------------
    # HTTP helpers (httpx)
    # ------------------------------------------------------------------

    async def _exchange_code_for_token(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """POST to the token endpoint and return the JSON body."""
        import httpx

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret

        async with httpx.AsyncClient(timeout=self.DEFAULT_HTTP_TIMEOUT) as client:
            resp = await client.post(
                token_url,
                data=data,
                headers={
                    "Accept": "application/json",
                    # Some IdPs (GitHub) rely on Accept to decide JSON vs urlencoded
                },
            )
        if resp.status_code >= 400:
            logger.error(
                "OAuth token endpoint returned %s: %s",
                resp.status_code,
                resp.text[:200],
            )
            raise OAuthCallbackError(f"Token exchange failed: {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            # Some legacy providers return urlencoded responses.
            from urllib.parse import parse_qs

            parsed = {k: v[0] for k, v in parse_qs(resp.text).items()}
            return parsed

    async def _http_get_json(
        self,
        url: str,
        *,
        bearer: str = "",
    ) -> dict[str, Any]:
        """GET a URL, expecting a JSON body."""
        if not url:
            return {}
        import httpx

        headers: dict[str, str] = {"Accept": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        async with httpx.AsyncClient(timeout=self.DEFAULT_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            logger.warning(
                "OAuth GET %s returned %s: %s",
                url,
                resp.status_code,
                resp.text[:200],
            )
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------------
    # JWT decode helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_id_token_unsafe(id_token: str) -> dict[str, Any]:
        """Decode an OIDC ``id_token`` without verifying the signature.

        Mirrors FAB's
        ``jwt.decode(id_token, options={"verify_signature": False})``
        used in :pymeth:`BaseSecurityManager._decode_and_validate_azure_jwt`
        when ``verify_signature`` is not configured.

        Use :meth:`OIDCAuthBackend.validate_id_token` instead for proper
        signature validation against the provider's JWKS.
        """
        if not id_token:
            return {}
        try:
            return pyjwt.decode(id_token, options={"verify_signature": False})
        except pyjwt.PyJWTError:
            logger.exception("Failed to decode id_token without verification")
            return {}

    # Static JWKS endpoint for Azure AD / Microsoft identity platform.
    # Mirrors FAB ``const.MICROSOFT_KEY_SET_URL``.
    MICROSOFT_KEY_SET_URL = "https://login.microsoftonline.com/common/discovery/keys"

    async def _validate_azure_jwt(self, id_token: str) -> dict[str, Any]:
        """Validate an Azure AD ``id_token`` against Microsoft's JWKS.

        Mirrors FAB ``_decode_and_validate_azure_jwt`` when
        ``verify_signature`` is set: the token must carry a valid signature
        from Microsoft's published key set. A validation failure rejects the
        login (raises) rather than silently degrading to an unverified
        decode.
        """
        if not id_token:
            return {}

        import asyncio

        def _validate() -> dict[str, Any]:
            jwk_client = pyjwt.PyJWKClient(self.MICROSOFT_KEY_SET_URL)
            signing_key = jwk_client.get_signing_key_from_jwt(id_token).key
            algorithms = [pyjwt.get_unverified_header(id_token).get("alg", "RS256")]
            # FAB's authlib path validates the signature; audience/issuer
            # checks are not enforced there, so we match that surface.
            return pyjwt.decode(
                id_token,
                signing_key,
                algorithms=algorithms,
                options={"verify_signature": True, "verify_aud": False},
            )

        try:
            return await asyncio.to_thread(_validate)
        except pyjwt.PyJWTError as exc:
            raise OAuthCallbackError(
                f"Azure id_token validation failed: {exc}"
            ) from exc
