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
"""Async SecurityManager — full reimplementation of the upstream SecurityManager.

Reads from the same ab_* tables as the upstream security layer but via
AsyncSession. Zero database migration needed. Used by AuthMiddleware
(short-lived session) and by controllers/guards (request-scoped session from DI).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import ssl
from typing import Any, cast, TYPE_CHECKING

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import NoResultFound, SQLAlchemyError
from sqlalchemy.orm import selectinload

from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import SupersetSecurityException
from superset.security.permissions import (
    ALL_DATABASE_ACCESS,
    ALL_DATASOURCE_ACCESS,
    ALL_QUERY_ACCESS,
    CATALOG_ACCESS,
    DATABASE_ACCESS,
    DATASOURCE_ACCESS,
    SCHEMA_ACCESS,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from superset.security.dao import AsyncSecurityDAO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns to extract integer IDs from permission strings
# ---------------------------------------------------------------------------
_DATASOURCE_PERM_RE = re.compile(r"^\[.+\]\.\[.+\]\(id:(?P<id>\d+)\)$")
_DATABASE_PERM_RE = re.compile(r"^\[.+\]\.\(id:(?P<id>\d+)\)$")


# ---------------------------------------------------------------------------
# Query-context modification check (guest user safety)
# ---------------------------------------------------------------------------


def _freeze_value(value: Any) -> str:
    """Deterministic JSON serialization for comparing column/metric sets.

    1:1 with ``freeze_value`` in ``superset_old/security/manager.py:182``.
    """
    return json.dumps(value, sort_keys=True)


def query_context_modified(query_context: Any) -> bool:
    """Check if a query context has been modified from its stored chart params.

    Used to prevent guest users from altering payloads to fetch data
    different from what was shared with them in dashboards.
    """
    form_data: dict[str, Any] | None = getattr(query_context, "form_data", None)
    stored_chart: Any | None = getattr(query_context, "slice_", None)

    # Native filter requests — no chart to compare against.
    if form_data is None or stored_chart is None:
        return False

    # Cannot request a different chart.
    if form_data.get("slice_id") != stored_chart.id:
        return True

    stored_query_context = (
        json.loads(cast(str, stored_chart.query_context))
        if stored_chart.query_context
        else None
    )

    # Compare columns and metrics in form_data with stored values.
    for key, equivalent in [
        ("metrics", ["metrics"]),
        ("columns", ["columns", "groupby"]),
        ("groupby", ["columns", "groupby"]),
        ("orderby", ["orderby"]),
    ]:
        requested_values = {_freeze_value(value) for value in form_data.get(key) or []}
        stored_values = {
            _freeze_value(value)
            for value in getattr(stored_chart, "params_dict", {}).get(key) or []
        }
        if not requested_values.issubset(stored_values):
            return True

        # Compare queries in query_context.
        queries = getattr(query_context, "queries", [])
        queries_values = {
            _freeze_value(value)
            for query in queries
            for value in getattr(query, key, []) or []
        }
        if stored_query_context:
            for sq in stored_query_context.get("queries") or []:
                for eq_key in equivalent:
                    stored_values.update(
                        {_freeze_value(value) for value in sq.get(eq_key) or []}
                    )

        if not queries_values.issubset(stored_values):
            return True

    return False


def build_async_security_manager(
    session: Any,
    settings: Any,
) -> "AsyncSecurityManager":
    """Build an :class:`AsyncSecurityManager` bound to ``session`` from settings.

    Single source of truth for the manager's construction so the Litestar DI
    provider (``superset.dependencies.provide_security_manager``) and
    non-request callers (e.g. the Celery ``load_explore_json_into_cache`` task,
    which must compute the same RLS cache key as the web process) build an
    identically-configured manager — otherwise their RLS cache keys would
    diverge and the explore_json cache would not be RLS-isolated.
    """
    from superset.security.dao import AsyncSecurityDAO

    feature_flags = getattr(settings, "feature_flags", {}) or {}
    embedded_enabled = getattr(
        settings, "embedded_superset", False
    ) or feature_flags.get("EMBEDDED_SUPERSET", False)
    return AsyncSecurityManager(
        dao=AsyncSecurityDAO(session),
        settings=settings,
        admin_role_name=settings.auth_role_admin,
        public_role_name=settings.auth_role_public,
        guest_role_name=settings.guest_role_name,
        dashboard_rbac_enabled=settings.dashboard_rbac,
        embedded_superset_enabled=embedded_enabled,
    )


class AsyncSecurityManager:
    """Async reimplementation of Superset's SecurityManager.

    Core methods are async equivalents of SupersetSecurityManager:
    - has_access / can_access: permission check
    - raise_for_access: permission check with exception
    - can_access_database / schema / datasource / dashboard
    - is_owner / is_admin
    - get_user_roles / get_schemas_accessible_by_user
    - get_rls_filters
    - guest token create/parse/validate
    - invalidate_user_cache

    All DB queries go through AsyncSecurityDAO.
    """

    _rls_warned: bool = False

    def __init__(
        self,
        dao: AsyncSecurityDAO,
        *,
        settings: Any = None,
        admin_role_name: str = "Admin",
        public_role_name: str = "Public",
        guest_role_name: str = "Guest",
        dashboard_rbac_enabled: bool = False,
        embedded_superset_enabled: bool = False,
    ) -> None:
        self.dao = dao
        # Store settings reference so that config-derived values are read
        # at call time (mirrors original get_conf() semantics) rather than
        # being snapshotted at construction time.
        self._settings = settings
        self._admin_role_name = admin_role_name
        self._public_role_name = public_role_name
        self._guest_role_name = guest_role_name
        self._dashboard_rbac_enabled = dashboard_rbac_enabled
        self._embedded_superset_enabled = embedded_superset_enabled

    @property
    def user_model(self) -> type:
        """Upstream ``SecurityManager.user_model`` contract (delegates to the DAO)."""
        return self.dao.user_model

    @property
    def role_model(self) -> type:
        """Upstream ``SecurityManager.role_model`` contract (delegates to the DAO)."""
        return self.dao.role_model

    @staticmethod
    def get_exclude_users_from_lists() -> list[str]:
        """Override to dynamically identify usernames to exclude from
        all UI dropdown lists (owners, created_by filters, etc.).

        Mirrors the original ``SupersetSecurityManager.get_exclude_users_from_lists``
        which is called as a fallback when ``EXCLUDE_USERS_FROM_LISTS`` config is None.

        :return: A list of usernames to exclude
        """
        return []

    async def find_user_by_id(self, user_id: int) -> Any | None:
        """Find a user by primary key (ab_user table)."""
        return await self.dao.get_user_by_id(user_id)

    async def find_role_by_id(self, role_id: int) -> Any | None:
        """Find a role by primary key (ab_role table)."""
        role_model: Any = self.dao.role_model
        stmt: Any = select(role_model).where(role_model.id == role_id)
        result = await self.dao.session.execute(stmt)
        return result.scalars().one_or_none()

    # ------------------------------------------------------------------
    # LDAP authentication
    # ------------------------------------------------------------------
    #
    # Ported 1:1 from the upstream
    # ``security/manager.py::auth_user_ldap`` and surrounding helpers
    # (``_search_ldap``, ``_bind_ldap``, ``_ldap_bind_indirect``,
    # ``_ldap_calculate_user_roles``, ``ldap_extract``, ``ldap_extract_list``).
    #
    # Implementation differences with the upstream code:
    #
    # * We use the pure-Python ``ldap3`` package (rather than the C-extension
    #   ``python-ldap``) so the call-graph stays free of build-time native
    #   dependencies.  ``ldap3`` is itself synchronous; we wrap blocking
    #   calls with :func:`asyncio.to_thread` to keep the controller
    #   coroutine-safe.
    # * Upstream reads ``current_app.config[...]``; we accept ``settings``
    #   (a :class:`SupersetSettings` instance) as an explicit parameter.
    # * Upstream calls ``self.add_user`` which lives on the upstream sqla
    #   manager; we inline the equivalent insert via :class:`AsyncSecurityDAO`.
    # * Role syncing follows ``AUTH_ROLES_SYNC_AT_LOGIN`` (default False)
    #   and ``AUTH_ROLES_MAPPING`` exactly as upstream.

    async def auth_user_ldap(  # noqa: C901
        self,
        username: str,
        password: str,
        *,
        settings: Any,
    ) -> Any | None:
        """Authenticate a user via LDAP.

        1:1 port of the upstream
        ``BaseSecurityManager.auth_user_ldap``.

        :param username: The username to authenticate
        :param password: The plaintext password to validate
        :param settings: A :class:`SupersetSettings` instance (LDAP config)
        :returns: The authenticated :class:`User` ORM object, or ``None``
            on failure (invalid creds, search miss, inactive user, etc.).
        """
        # If no username is provided, go away.
        if not username:
            return None

        # ``ldap3`` is the only optional dependency here.  Detect a
        # missing install up front so the failure mode is unambiguous.
        try:
            import ldap3
            from ldap3.core.exceptions import LDAPException
        except ImportError:
            logger.error("ldap3 library is not installed")
            return None

        # Search the metadata DB for the user.
        user = await self.dao.get_user_by_username(username)

        # If user exists but is inactive, deny silently — mirrors upstream.
        if user is not None and not getattr(user, "active", True):
            return None

        # If user is unknown and self-registration is disabled, deny.
        # Mirrors upstream: ``if (not user) and (not self.auth_user_registration)``.
        auth_user_registration = bool(
            getattr(settings, "auth_user_registration", False)
        )
        if user is None and not auth_user_registration:
            return None

        # Required: AUTH_LDAP_SERVER must be configured.
        ldap_server_uri: str = getattr(settings, "auth_ldap_server", "") or ""
        if not ldap_server_uri:
            logger.error(
                "AUTH_LDAP_SERVER must be configured to use LDAP authentication"
            )
            return None

        try:
            ldap_result = await self._ldap_authenticate_and_search(
                ldap_module=ldap3,
                ldap_server_uri=ldap_server_uri,
                username=username,
                password=password,
                settings=settings,
            )
        except LDAPException as exc:
            logger.error("LDAP error during authentication: %s", exc)
            return None

        if ldap_result is None:
            # Bind failed or search came up empty — auth failure.
            # Mirror upstream by recording a failed-login stat for known users.
            if user is not None:
                await self._update_user_auth_stat(user, success=False)
            return None

        user_dn, user_attributes = ldap_result

        # Sync roles for existing users when AUTH_ROLES_SYNC_AT_LOGIN is on.
        if (
            user is not None
            and user_attributes
            and getattr(settings, "auth_roles_sync_at_login", False)
        ):
            user.roles = await self._ldap_calculate_user_roles(
                user_attributes, settings=settings
            )
            logger.debug(
                "Calculated new roles for user '%s' as: %s",
                user_dn,
                [r.name for r in user.roles],
            )

        # Self-register new LDAP users if enabled.
        if user is None and user_attributes and auth_user_registration:
            first_name = self._ldap_extract(
                user_attributes,
                getattr(settings, "auth_ldap_firstname_field", "givenName"),
                "",
            )
            last_name = self._ldap_extract(
                user_attributes,
                getattr(settings, "auth_ldap_lastname_field", "sn"),
                "",
            )
            email = self._ldap_extract(
                user_attributes,
                getattr(settings, "auth_ldap_email_field", "mail"),
                f"{username}@email.notfound",
            )
            roles = await self._ldap_calculate_user_roles(
                user_attributes, settings=settings
            )
            user = await self._register_user(
                username=username,
                first_name=first_name,
                last_name=last_name,
                email=email,
                roles=roles,
            )
            if user is None:
                logger.info("LDAP self-registration failed for '%s'", username)
                return None
            logger.debug("New LDAP user registered: %s", username)

        if user is None:
            return None

        await self._update_user_auth_stat(user, success=True)
        return user

    async def auth_user_remote_user(
        self,
        username: str,
        *,
        settings: Any,
    ) -> Any | None:
        """Authenticate a user resolved from the ``REMOTE_USER`` variable.

        1:1 port of the upstream
        ``BaseSecurityManager.auth_user_remote_user``
        (upstream security/manager.py:1407-1435).
        """
        user = await self.dao.get_user_by_username(username)

        # User does not exist, create one if auto user registration.
        if user is None and getattr(settings, "auth_user_registration", False):
            registration_role_name = getattr(
                settings, "auth_user_registration_role", "Public"
            )
            registration_role = await self.dao.get_role_by_name(registration_role_name)
            user = await self._register_user(
                # All we have is REMOTE_USER, so we set
                # the other fields to blank.
                username=username,
                first_name=username,
                last_name="-",
                email=username + "@email.notfound",
                roles=[registration_role] if registration_role else None,
            )

        # If user does not exist on the DB and not auto user registration,
        # or user is inactive, go away.
        elif user is None or not getattr(user, "active", True):
            logger.info("Login Failed for user: %s", username)
            return None

        if user is None:
            return None

        await self._update_user_auth_stat(user, success=True)
        return user

    async def _oauth_calculate_user_roles(
        self,
        userinfo: dict[str, Any],
        *,
        settings: Any,
    ) -> list[Any]:
        """Map OAuth userinfo to a list of :class:`Role` objects.

        1:1 port of the upstream
        ``BaseSecurityManager._oauth_calculate_user_roles``
        (upstream security/manager.py:1437-1467):

        * ``AUTH_ROLES_MAPPING`` translates the IdP's ``role_keys`` claim
          into one or more Superset role names (``get_roles_from_keys``).
        * When ``AUTH_USER_REGISTRATION`` is on, the configured
          ``AUTH_USER_REGISTRATION_ROLE`` is appended — optionally resolved
          dynamically via ``AUTH_USER_REGISTRATION_ROLE_JMESPATH``.
        """
        user_role_objects: dict[int, Any] = {}

        # apply AUTH_ROLES_MAPPING (upstream ``get_roles_from_keys``)
        roles_mapping = getattr(settings, "auth_roles_mapping", {}) or {}
        if roles_mapping:
            user_role_keys = set(userinfo.get("role_keys", []) or [])
            for role_key, fab_role_names in roles_mapping.items():
                if role_key not in user_role_keys:
                    continue
                for fab_role_name in fab_role_names:
                    fab_role = await self.dao.get_role_by_name(fab_role_name)
                    if fab_role is not None:
                        user_role_objects[fab_role.id] = fab_role
                    else:
                        logger.warning(
                            "Can't find role specified in AUTH_ROLES_MAPPING: %s",
                            fab_role_name,
                        )

        # apply AUTH_USER_REGISTRATION_ROLE
        if getattr(settings, "auth_user_registration", False):
            registration_role_name = getattr(
                settings, "auth_user_registration_role", "Public"
            )

            # if AUTH_USER_REGISTRATION_ROLE_JMESPATH is set, use it for the
            # registration role
            jmespath_expr = getattr(
                settings, "auth_user_registration_role_jmespath", None
            )
            if jmespath_expr:
                try:
                    import jmespath

                    registration_role_name = jmespath.search(jmespath_expr, userinfo)
                except ImportError:
                    logger.error(
                        "jmespath is not installed; cannot evaluate "
                        "AUTH_USER_REGISTRATION_ROLE_JMESPATH"
                    )

            fab_role = await self.dao.get_role_by_name(registration_role_name)
            if fab_role is not None:
                user_role_objects.setdefault(fab_role.id, fab_role)
            else:
                logger.warning(
                    "Can't find AUTH_USER_REGISTRATION role: %s",
                    registration_role_name,
                )

        return list(user_role_objects.values())

    async def auth_user_oauth(
        self,
        userinfo: dict[str, Any],
        *,
        settings: Any,
    ) -> Any | None:
        """Authenticate a user via an OAuth userinfo document.

        1:1 port of the upstream
        ``BaseSecurityManager.auth_user_oauth``
        (upstream security/manager.py:1469-1526).

        :param userinfo: dict with user information
            (keys are the same as User model columns)
        :param settings: A :class:`SupersetSettings` instance (auth config)
        """
        # extract the username from `userinfo`
        if "username" in userinfo:
            username = userinfo["username"]
        elif "email" in userinfo:
            username = userinfo["email"]
        else:
            logger.error("OAUTH userinfo does not have username or email %s", userinfo)
            return None

        # If username is empty, go away
        if (username is None) or username == "":
            return None

        # Search the DB for this user
        user = await self.dao.get_user_by_username(username)

        # If user is not active, go away
        if user is not None and not getattr(user, "active", True):
            return None

        # If user is not registered, and not self-registration, go away
        auth_user_registration = bool(
            getattr(settings, "auth_user_registration", False)
        )
        if user is None and not auth_user_registration:
            return None

        # Sync the user's roles
        if user is not None and getattr(settings, "auth_roles_sync_at_login", False):
            user.roles = await self._oauth_calculate_user_roles(
                userinfo, settings=settings
            )
            logger.debug(
                "Calculated new roles for user='%s' as: %s", username, user.roles
            )

        # If the user is new, register them
        if user is None and auth_user_registration:
            user = await self._register_user(
                username=username,
                first_name=userinfo.get("first_name", ""),
                last_name=userinfo.get("last_name", ""),
                email=userinfo.get("email", "") or f"{username}@email.notfound",
                roles=await self._oauth_calculate_user_roles(
                    userinfo, settings=settings
                ),
            )
            logger.debug("New user registered: %s", user)

            # If user registration failed, go away
            if user is None:
                logger.error("Error creating a new OAuth user %s", username)
                return None

        # LOGIN SUCCESS (only if user is now registered)
        if user:
            await self._update_user_auth_stat(user, success=True)
            return user
        return None

    async def _ldap_authenticate_and_search(  # noqa: C901
        self,
        *,
        ldap_module: Any,
        ldap_server_uri: str,
        username: str,
        password: str,
        settings: Any,
    ) -> tuple[str | None, dict[str, list[bytes]] | None] | None:
        """Establish the LDAP connection and resolve ``(user_dn, attrs)``.

        Encapsulates both the indirect-bind (service account search) and
        direct-bind flows.  All blocking ``ldap3`` calls are dispatched
        through :func:`asyncio.to_thread` so the calling coroutine never
        stalls the event loop.

        Returns ``None`` on authentication failure (bind failed, search
        miss, etc.), or a ``(user_dn, user_attributes)`` tuple on success.
        ``user_dn`` may legitimately be ``None`` in the direct-bind flow
        when ``AUTH_LDAP_SEARCH`` is not configured — mirrors upstream which
        leaves ``user_dn = None`` in that path.
        """

        bind_user: str = getattr(settings, "auth_ldap_bind_user", "") or ""
        ldap_search: str = getattr(settings, "auth_ldap_search", "") or ""

        def _do_ldap_flow() -> (  # noqa: C901
            tuple[str | None, dict[str, list[bytes]] | None] | None
        ):
            # Build a TLS context that mirrors the upstream knobs.
            tls = self._build_ldap_tls(ldap_module, settings)

            server = ldap_module.Server(
                ldap_server_uri,
                use_ssl=False,
                tls=tls,
                get_info=ldap_module.NONE,
            )

            # Open the connection without binding yet — we'll bind below.
            con = ldap_module.Connection(
                server,
                auto_bind=False,
                client_strategy=ldap_module.SYNC,
                raise_exceptions=False,
            )
            # ``referrals`` is set to False (mirrors upstream's
            # ``set_option(OPT_REFERRALS, 0)``).
            con.referrals = False

            # ``open()`` initialises the socket.  ``start_tls()`` is only
            # called when AUTH_LDAP_USE_TLS is set and the URI is plain LDAP.
            con.open()
            if getattr(settings, "auth_ldap_use_tls", False):
                if not con.start_tls():
                    logger.error(
                        "LDAP TLS upgrade failed against server '%s'",
                        ldap_server_uri,
                    )
                    con.unbind()
                    return None

            try:
                # Define defaults — mirror upstream lines 1275-1276
                # (``user_dn = None``; ``user_attributes = {}``).
                user_dn: str | None = None
                user_attributes: dict[str, list[bytes]] | None = {}

                # Flow 1: indirect bind (service account performs search).
                if bind_user:
                    if not self._ldap_bind_indirect_sync(con, settings):
                        return None

                    if not ldap_search:
                        logger.error(
                            "AUTH_LDAP_SEARCH must be set when using"
                            " AUTH_LDAP_BIND_USER"
                        )
                        return None

                    user_dn, user_attributes = self._search_ldap_sync(
                        con, username, settings
                    )
                    if user_dn is None:
                        logger.info("LDAP search returned no entry for '%s'", username)
                        return None

                    if not self._bind_ldap_sync(con, user_dn, password):
                        logger.info(
                            "LDAP bind FAILED for resolved DN of user '%s'",
                            username,
                        )
                        return None

                    return user_dn, user_attributes

                # Flow 2: direct bind (end-user creds drive both bind & search).
                bind_username = username
                if append_domain := getattr(settings, "auth_ldap_append_domain", ""):
                    bind_username = f"{bind_username}@{append_domain}"
                if username_format := getattr(
                    settings, "auth_ldap_username_format", ""
                ):
                    bind_username = username_format % bind_username

                if not self._bind_ldap_sync(con, bind_username, password):
                    logger.info(
                        "LDAP bind FAILED for direct username '%s'", bind_username
                    )
                    return None

                # Mirror upstream: in the direct-bind flow ``user_dn`` stays
                # ``None`` unless ``AUTH_LDAP_SEARCH`` is configured —
                # ``bind_username`` is NOT a DN and must not be returned
                # as one (upstream code: ``security/manager.py``
                # lines 1275, 1313-1356).
                if ldap_search:
                    user_dn, user_attributes = self._search_ldap_sync(
                        con, username, settings
                    )
                    if user_dn is None:
                        logger.info("LDAP search returned no entry for '%s'", username)
                        return None
                return user_dn, user_attributes
            finally:
                try:
                    con.unbind()
                except Exception:  # noqa: BLE001, S110
                    pass  # best-effort cleanup

        return await asyncio.to_thread(_do_ldap_flow)

    @staticmethod
    def _build_ldap_tls(ldap_module: Any, settings: Any) -> Any | None:
        """Construct a ``ldap3.Tls`` instance from upstream-style TLS knobs.

        Mirrors the ``ldap.set_option(OPT_X_TLS_*)`` calls in
        ``BaseSecurityManager.auth_user_ldap``.  Returns ``None`` when no
        TLS configuration is in effect.
        """
        cacertdir = getattr(settings, "auth_ldap_tls_cacertdir", "") or ""
        cacertfile = getattr(settings, "auth_ldap_tls_cacertfile", "") or ""
        certfile = getattr(settings, "auth_ldap_tls_certfile", "") or ""
        keyfile = getattr(settings, "auth_ldap_tls_keyfile", "") or ""
        allow_self_signed = bool(
            getattr(settings, "auth_ldap_allow_self_signed", False)
        )
        tls_demand = bool(getattr(settings, "auth_ldap_tls_demand", False))
        use_tls = bool(getattr(settings, "auth_ldap_use_tls", False))

        if not (
            cacertdir
            or cacertfile
            or certfile
            or keyfile
            or allow_self_signed
            or tls_demand
            or use_tls
        ):
            return None

        if allow_self_signed:
            validate = ssl.CERT_NONE
        elif tls_demand:
            validate = ssl.CERT_REQUIRED
        else:
            validate = ssl.CERT_OPTIONAL

        return ldap_module.Tls(
            local_private_key_file=keyfile or None,
            local_certificate_file=certfile or None,
            ca_certs_file=cacertfile or None,
            ca_certs_path=cacertdir or None,
            validate=validate,
        )

    @staticmethod
    def _bind_ldap_sync(con: Any, dn: str, password: str) -> bool:
        """Validate ``dn``/``password`` against the live LDAP connection.

        Mirrors :pymeth:`BaseSecurityManager._ldap_bind` exactly: returns
        ``True`` on a successful bind, ``False`` on invalid credentials.
        """
        logger.debug("LDAP bind TRY with DN: '%s'", dn)
        try:
            ok = con.rebind(user=dn, password=password)
        except Exception as exc:  # noqa: BLE001
            logger.debug("LDAP bind raised: %s", exc)
            return False
        if ok:
            logger.debug("LDAP bind SUCCESS with DN: '%s'", dn)
            return True
        logger.debug("LDAP bind FAILED for DN: '%s'", dn)
        return False

    @staticmethod
    def _ldap_bind_indirect_sync(con: Any, settings: Any) -> bool:
        """Bind as ``AUTH_LDAP_BIND_USER`` for service-account search.

        Mirrors :pymeth:`BaseSecurityManager._ldap_bind_indirect`.
        """
        bind_user: str = getattr(settings, "auth_ldap_bind_user", "") or ""
        bind_password: str = getattr(settings, "auth_ldap_bind_password", "") or ""
        assert bind_user, "AUTH_LDAP_BIND_USER must be set"

        logger.debug("LDAP bind indirect TRY with username: '%s'", bind_user)
        try:
            ok = con.rebind(user=bind_user, password=bind_password)
        except Exception as exc:  # noqa: BLE001
            logger.error("LDAP indirect bind raised: %s", exc)
            return False
        if not ok:
            logger.error(
                "AUTH_LDAP_BIND_USER and AUTH_LDAP_BIND_PASSWORD are"
                " not valid LDAP bind credentials"
            )
            return False
        logger.debug("LDAP bind indirect SUCCESS with username: '%s'", bind_user)
        return True

    @staticmethod
    def _search_ldap_sync(  # noqa: C901
        con: Any,
        username: str,
        settings: Any,
    ) -> tuple[str | None, dict[str, list[bytes]] | None]:
        """Search LDAP for a single user entry.

        Mirrors :pymeth:`BaseSecurityManager._search_ldap`.  Returns
        ``(user_dn, attributes_dict)`` or ``(None, None)`` if the search
        produced zero or multiple matches.
        """
        ldap_search = getattr(settings, "auth_ldap_search", "") or ""
        assert ldap_search, "AUTH_LDAP_SEARCH must be set"

        uid_field = getattr(settings, "auth_ldap_uid_field", "uid")
        ldap_search_filter = getattr(settings, "auth_ldap_search_filter", "") or ""
        if ldap_search_filter:
            filter_str = f"(&{ldap_search_filter}({uid_field}={username}))"
        else:
            filter_str = f"({uid_field}={username})"

        request_fields = [
            getattr(settings, "auth_ldap_firstname_field", "givenName"),
            getattr(settings, "auth_ldap_lastname_field", "sn"),
            getattr(settings, "auth_ldap_email_field", "mail"),
        ]
        roles_mapping = getattr(settings, "auth_roles_mapping", {}) or {}
        if roles_mapping:
            request_fields.append(
                getattr(settings, "auth_ldap_group_field", "memberOf")
            )

        logger.debug(
            "LDAP search for '%s' with fields %s in scope '%s'",
            filter_str,
            request_fields,
            ldap_search,
        )

        # ldap3 returns raw results in `con.response` after `search()`.
        ok = con.search(
            search_base=ldap_search,
            search_filter=filter_str,
            search_scope="SUBTREE",
            attributes=request_fields,
        )
        if not ok:
            logger.debug("LDAP search returned no results")
            return None, None

        # Filter out search-continuation referrals.  ldap3 returns the
        # per-entry ``attributes`` as a ``CaseInsensitiveDict`` which is NOT
        # a ``dict`` subclass, so test for the more general ``Mapping`` —
        # otherwise every real LDAP entry is discarded and indirect-bind
        # login silently fails.  (Upstream checks ``isinstance(attrs, dict)``
        # because python-ldap's ``search_s`` yields plain dicts.)
        from collections.abc import Mapping

        entries: list[Any] = [
            entry
            for entry in (con.response or [])
            if entry.get("type") == "searchResEntry"
            and isinstance(entry.get("attributes"), Mapping)
        ]
        if len(entries) > 1:
            logger.error(
                "LDAP search for '%s' in scope '%s' returned multiple results",
                filter_str,
                ldap_search,
            )
            return None, None
        if not entries:
            return None, None

        entry = entries[0]
        user_dn = entry.get("dn")
        # Normalise attribute values to ``list[bytes]`` so the same
        # downstream extraction logic works for both ldap3 and python-ldap.
        raw_attrs: dict[str, Any] = entry.get("attributes", {}) or {}
        normalised: dict[str, list[bytes]] = {}
        for key, value in raw_attrs.items():
            values: list[Any]
            if isinstance(value, list):
                values = value
            elif value is None:
                values = []
            else:
                values = [value]

            byte_values: list[bytes] = []
            for v in values:
                if isinstance(v, bytes):
                    byte_values.append(v)
                elif isinstance(v, str):
                    byte_values.append(v.encode("utf-8"))
                else:
                    byte_values.append(str(v).encode("utf-8"))
            normalised[key] = byte_values
        return user_dn, normalised

    async def _search_ldap(
        self,
        ldap: Any,
        con: Any,
        username: str,
        *,
        settings: Any,
    ) -> tuple[str | None, dict[str, list[bytes]] | None]:
        """Async wrapper around :meth:`_search_ldap_sync`.

        Exposed as ``async`` to satisfy the contract documented in the
        upstream port plan; delegates to the blocking helper via
        :func:`asyncio.to_thread`.
        """
        del ldap  # signature-compatibility with upstream
        return await asyncio.to_thread(self._search_ldap_sync, con, username, settings)

    async def _bind_ldap(
        self,
        ldap: Any,
        con: Any,
        username: str,
        password: str,
    ) -> bool:
        """Async wrapper around :meth:`_bind_ldap_sync`."""
        del ldap  # signature-compatibility with upstream
        return await asyncio.to_thread(self._bind_ldap_sync, con, username, password)

    async def _ldap_bind_indirect(
        self,
        ldap: Any,
        con: Any,
        *,
        settings: Any,
    ) -> bool:
        """Async wrapper around :meth:`_ldap_bind_indirect_sync`."""
        del ldap  # signature-compatibility with upstream
        return await asyncio.to_thread(self._ldap_bind_indirect_sync, con, settings)

    @staticmethod
    def _ldap_extract(
        ldap_dict: dict[str, list[bytes]],
        field_name: str,
        fallback: str,
    ) -> str:
        """Extract the first value of an LDAP attribute as ``str``.

        Mirrors the upstream :pymeth:`BaseSecurityManager.ldap_extract`.
        """
        raw_value = ldap_dict.get(field_name) or [b""]
        first = raw_value[0]
        if isinstance(first, bytes):
            try:
                decoded = first.decode("utf-8")
            except UnicodeDecodeError:
                decoded = ""
        else:
            decoded = str(first)
        return decoded or fallback

    async def _ldap_extract_list(
        self,
        attributes: dict[str, list[bytes]],
        name: str,
    ) -> list[str]:
        """Extract a multi-valued LDAP attribute as ``list[str]``.

        Mirrors the upstream :pymeth:`BaseSecurityManager.ldap_extract_list`.
        Empty strings are filtered out, exactly as in the original.
        """
        raw_list = attributes.get(name) or []
        result: list[str] = []
        for raw in raw_list:
            if isinstance(raw, bytes):
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            else:
                text = str(raw)
            if text:
                result.append(text)
        return result

    async def _ldap_calculate_user_roles(
        self,
        user_attributes: dict[str, list[bytes]],
        *,
        settings: Any,
    ) -> list[Any]:
        """Map LDAP attributes to a list of :class:`Role` objects.

        Ports :pymeth:`BaseSecurityManager._ldap_calculate_user_roles` 1:1:

        * ``AUTH_ROLES_MAPPING`` translates LDAP group DNs (or any other
          configured field) into one or more Superset role names.
        * When ``AUTH_USER_REGISTRATION`` is on, the configured
          ``AUTH_USER_REGISTRATION_ROLE`` is appended.
        """
        user_role_objects: dict[int, Any] = {}

        roles_mapping = getattr(settings, "auth_roles_mapping", {}) or {}
        if roles_mapping:
            group_field = getattr(settings, "auth_ldap_group_field", "memberOf")
            user_role_keys = set(
                await self._ldap_extract_list(user_attributes, group_field)
            )
            for role_key, fab_role_names in roles_mapping.items():
                if role_key not in user_role_keys:
                    continue
                for fab_role_name in fab_role_names:
                    fab_role = await self.dao.get_role_by_name(fab_role_name)
                    if fab_role is not None:
                        user_role_objects[fab_role.id] = fab_role
                    else:
                        logger.warning(
                            "Can't find role specified in AUTH_ROLES_MAPPING: %s",
                            fab_role_name,
                        )

        if getattr(settings, "auth_user_registration", False):
            registration_role_name = getattr(
                settings, "auth_user_registration_role", "Public"
            )
            fab_role = await self.dao.get_role_by_name(registration_role_name)
            if fab_role is not None:
                user_role_objects.setdefault(fab_role.id, fab_role)
            else:
                logger.warning(
                    "Can't find AUTH_USER_REGISTRATION role: %s",
                    registration_role_name,
                )

        return list(user_role_objects.values())

    async def _register_user(
        self,
        *,
        username: str,
        first_name: str,
        last_name: str,
        email: str,
        roles: list[Any] | None = None,
    ) -> Any | None:
        """Insert a new ``ab_user`` row for an externally authenticated user.

        Mirrors the upstream :pymeth:`SecurityManager.add_user` but skips the
        password hashing step — LDAP/OAuth users authenticate against the
        external IdP, not the local password column.  ``ab_user.password``
        is left ``NULL`` so the row cannot be used for DB-auth login.
        """
        user_model: Any = self.dao.user_model
        session = self.dao.session

        # Mirror upstream AuditMixin defaults: created/changed timestamps are
        # naive local time (``datetime.now()`` — see the upstream
        # ``security/sqla/models.py`` lines 177-181).
        # ``created_by_fk``/``changed_by_fk`` default to
        # ``cls.get_user_id`` which returns the current user's id if available,
        # else ``None`` — for self-registration there is no admin user
        # so both are ``None`` (the columns are nullable).
        now = dt.datetime.now()
        new_user = user_model(
            first_name=first_name,
            last_name=last_name,
            username=username,
            email=email,
            active=True,
            password=None,
            login_count=0,
            fail_login_count=0,
            created_on=now,
            changed_on=now,
        )
        # Set audit FKs explicitly — these columns are inherited from
        # the upstream ``AuditMixin`` and are nullable.  The Liteset User model
        # may not declare them as ORM-mapped columns, so we assign via
        # ``setattr`` so SQLAlchemy persists them only when the column
        # exists on the mapped table.
        if hasattr(user_model, "created_by_fk"):
            new_user.created_by_fk = None
        if hasattr(user_model, "changed_by_fk"):
            new_user.changed_by_fk = None
        if roles:
            new_user.roles = list(roles)

        try:
            session.add(new_user)
            await session.flush()
            await session.commit()
        except SQLAlchemyError:
            logger.exception("Failed to register external user '%s'", username)
            await session.rollback()
            return None
        # Re-fetch with eagerly loaded roles to match ``get_user_by_*``
        # contract elsewhere in the SM.
        return await self.dao.get_user_by_id(new_user.id)

    async def _update_user_auth_stat(self, user: Any, *, success: bool) -> None:
        """Increment login/failure counters and persist them.

        Ports :pymeth:`BaseSecurityManager.update_user_auth_stat` 1:1.
        Failures are counted but never raise — auth stat bookkeeping
        must not block the login response.
        """
        try:
            if not getattr(user, "login_count", None):
                user.login_count = 0
            if not getattr(user, "fail_login_count", None):
                user.fail_login_count = 0
            if success:
                user.login_count = (user.login_count or 0) + 1
                # Upstream uses naive local time; mirror exactly.
                user.last_login = dt.datetime.now()
                user.fail_login_count = 0
            else:
                user.fail_login_count = (user.fail_login_count or 0) + 1
            user.changed_on = dt.datetime.now()
            await self.dao.session.commit()
        except SQLAlchemyError:
            logger.exception(
                "Failed to update auth stats for user_id=%s",
                getattr(user, "id", None),
            )
            try:
                await self.dao.session.rollback()
            except SQLAlchemyError:
                pass

    def is_admin(self, user: Any) -> bool:
        """Check if user has the Admin role."""
        roles = getattr(user, "roles", [])
        return any(getattr(r, "name", None) == self._admin_role_name for r in roles)

    async def has_access(
        self,
        permission_name: str,
        view_name: str,
        *,
        user: Any,
    ) -> bool:
        """Check if user has a specific permission on a view/resource.

        Admin users bypass all permission checks.
        """
        if self.is_admin(user):
            return True
        # Fast path: check pre-resolved permissions (CachedUser, GuestUser)
        user_perms = getattr(user, "permissions", None)
        if isinstance(user_perms, (set, frozenset)):
            return (permission_name, view_name) in user_perms
        # Slow path: DAO query for ORM users. Resolve permissions across both
        # the user's direct roles AND group-inherited roles — 1:1 with upstream's
        # ``_has_view_access`` which walks ``ab_user_role`` + ``ab_user_group``
        # → ``ab_group_role``. Without the group roles a user whose access is
        # granted solely via a group would get a spurious 403.
        role_ids: set[int] = {r.id for r in getattr(user, "roles", [])}
        user_id = getattr(user, "id", None)
        if user_id is not None:
            for grp in await self.dao.get_user_groups(user_id):
                for grp_role in await self.dao.get_group_roles(grp[0]):
                    role_ids.add(grp_role[0])
        if not role_ids:
            return False
        return await self.dao.has_permission_view(
            permission_name, view_name, role_ids=list(role_ids)
        )

    async def can_access(
        self,
        permission_name: str,
        view_name: str,
        *,
        user: Any,
    ) -> bool:
        """Alias for has_access (matches Superset API)."""
        return await self.has_access(permission_name, view_name, user=user)

    async def get_user_roles(self, user: Any) -> list[Any]:
        """Get all roles for a user."""
        return await self.dao.get_user_roles(user)

    async def raise_for_access(  # noqa: C901, PLR0912, PLR0915
        self,
        *,
        user: Any,
        database: Any | None = None,
        catalog: str | None = None,
        schema: str | None = None,
        table: Any | None = None,
        datasource: Any | None = None,
        dashboard: Any | None = None,
        chart: Any | None = None,
        query: Any | None = None,
        query_context: Any | None = None,
        viz: Any | None = None,
        sql: str | None = None,
        template_params: dict[str, Any] | None = None,
    ) -> None:
        """Raise SupersetSecurityException if user lacks access.

        Mirrors the original ``SupersetSecurityManager.raise_for_access``
        with the same ordering:
        1. sql + database -> synthetic Query creation
        2. table/query path (database -> catalog -> schema -> datasource)
        3. Guest query_context modification check
        4. datasource/query_context/viz path (with dashboard RBAC fallback)
        5. dashboard path
        6. chart path

        :param database: The Superset database
        :param datasource: The Superset datasource
        :param query: The SQL Lab query
        :param query_context: The query context
        :param table: The Superset table (requires database)
        :param viz: The visualization
        :param sql: The SQL string (requires database)
        :param catalog: Optional catalog name
        :param schema: Optional schema name
        :param template_params: Optional template parameters for Jinja templating
        :raises SupersetSecurityException: If the user cannot access the resource
        """
        if self.is_admin(user):
            return

        # ------------------------------------------------------------------
        # Synthetic Query from raw SQL  (original lines 2315-2324)
        # ------------------------------------------------------------------
        if sql and database:
            from superset.models.sql_lab import Query as QueryModel
            from superset.utils.core import shortid

            query = QueryModel(
                database=database,
                sql=sql,
                schema=schema,
                catalog=catalog,
                client_id=shortid()[:10],
                user_id=getattr(user, "id", None),
            )
            # Expunge from session so it's not persisted — mirrors
            # ``self.session.expunge(query)`` in the original.
            try:
                from sqlalchemy import inspect as sa_inspect

                state = sa_inspect(query, raiseerr=False)
                if state is not None and state.session is not None:
                    state.session.expunge(query)
            except Exception:  # noqa: BLE001, S110
                pass  # Query may not be in a session — safe to ignore

        # ------------------------------------------------------------------
        # Path 1: database + table  OR  query
        # Mirrors original lines 2326-2397
        # ------------------------------------------------------------------
        if (database and table) or query:
            if query:
                database = getattr(query, "database", database)

            database = cast(Any, database)
            default_catalog = (
                database.db_engine_spec.get_default_catalog(database)
                if hasattr(database, "db_engine_spec")
                else None
            )

            if await self.can_access_database(database, user=user):
                return

            tables: set[Any] = set()
            if query:
                # Extract all referenced tables from the SQL via Jinja
                # rendering + SQLGlot parsing.
                # Mirrors original lines 2336-2355.
                # IMPORTANT: no try/except -- if parsing fails, the exception
                # propagates out of raise_for_access (fail-CLOSED), matching
                # the original's behaviour.  Silently swallowing the error
                # would leave ``tables`` empty and skip all table-level
                # permission checks.
                default_schema = self._get_default_schema_for_query(
                    database, query, template_params
                )
                from superset.sql.parse import process_jinja_sql

                tables = {
                    table_.qualify(
                        catalog=getattr(query, "catalog", None) or default_catalog,
                        schema=default_schema,
                    )
                    for table_ in process_jinja_sql(
                        query.sql, database, template_params
                    ).tables
                }

            elif table:
                # Make sure table has the default catalog, if not specified.
                if hasattr(table, "qualify"):
                    table = table.qualify(catalog=default_catalog)
                tables = {table}

            denied: set[Any] = set()
            for table_ in tables:
                # Catalog-level check
                catalog_perm = self.get_catalog_perm(
                    getattr(database, "database_name", ""),
                    getattr(table_, "catalog", None) or "",
                )
                if catalog_perm and await self.can_access(
                    CATALOG_ACCESS, catalog_perm, user=user
                ):
                    continue

                # Schema-level check
                schema_perm = self.get_schema_perm(
                    database,
                    getattr(table_, "schema", None) or "",
                    catalog=getattr(table_, "catalog", None),
                )
                if schema_perm and await self.can_access(
                    SCHEMA_ACCESS, schema_perm, user=user
                ):
                    continue

                # Datasource-level check + ownership
                table_name = getattr(table_, "table", None) or str(table_)
                if await self._can_access_table_datasource(
                    database,
                    table_name,
                    getattr(table_, "schema", None),
                    getattr(table_, "catalog", None),
                    user=user,
                ):
                    continue

                denied.add(table_)

            if denied:
                raise SupersetSecurityException(
                    self.get_table_access_error_object(denied)
                )

        # ------------------------------------------------------------------
        # Path 2: Guest user query_context modification check
        # Mirrors original lines 2399-2412.
        # MUST come between table/query and datasource/query_context/viz.
        # ------------------------------------------------------------------
        if (
            query_context
            and self.is_guest_user(user)
            and query_context_modified(query_context)
        ):
            raise SupersetSecurityException(
                SupersetError(
                    error_type=SupersetErrorType.DASHBOARD_SECURITY_ACCESS_ERROR,
                    message="Guest user cannot modify chart payload",
                    level=ErrorLevel.WARNING,
                )
            )

        # ------------------------------------------------------------------
        # Path 3: datasource / query_context / viz
        # Mirrors original lines 2414-2485 — includes dashboard RBAC fallback.
        # ------------------------------------------------------------------
        if datasource or query_context or viz:
            form_data: dict[str, Any] | None = None

            if query_context:
                datasource = getattr(query_context, "datasource", datasource)
                form_data = getattr(query_context, "form_data", None)
            elif viz:
                datasource = getattr(viz, "datasource", datasource)
                form_data = getattr(viz, "form_data", None)

            assert datasource

            # Check direct access first, then dashboard RBAC fallback
            has_direct_access = await self._can_access_datasource_schema(
                datasource, user=user
            ) or await self.can_access(
                DATASOURCE_ACCESS,
                getattr(datasource, "perm", "") or "",
                user=user,
            )
            if not has_direct_access:
                # ``is_owner`` reads ``datasource.owners`` synchronously — load
                # it first so a bare-fetched datasource doesn't trip a sync
                # lazy-load (MissingGreenlet) on the async session. Mirrors
                # ``can_access_datasource`` which preloads owners before
                # ``is_owner``.
                await self._ensure_relationship_loaded(datasource, "owners")
                has_direct_access = self.is_owner(datasource, user)

            if not has_direct_access:
                # Dashboard RBAC fallback: when user lacks direct datasource
                # access but has access to a dashboard using it (via
                # form_data.dashboardId), access is granted.
                # Mirrors original lines 2435-2481.
                dashboard_fallback = False
                if form_data and (dashboard_id := form_data.get("dashboardId")):
                    dashboard_ = await self._get_dashboard_by_id(dashboard_id)
                    if dashboard_ is not None:
                        # Check if dashboard RBAC or embedded guest applies
                        rbac_or_guest = (
                            self._dashboard_rbac_enabled
                            and getattr(dashboard_, "roles", [])
                        ) or (
                            self._embedded_superset_enabled and self.is_guest_user(user)
                        )

                        if rbac_or_guest:
                            # Validate the specific resource (native filter,
                            # chart, or drill-by)
                            resource_valid = False

                            if form_data.get("type") == "NATIVE_FILTER":
                                # Native filter validation
                                native_filter_id = form_data.get("native_filter_id")
                                json_metadata_raw = getattr(
                                    dashboard_, "json_metadata", None
                                )
                                if native_filter_id and json_metadata_raw:
                                    try:
                                        json_metadata = json.loads(json_metadata_raw)
                                    except (json.JSONDecodeError, TypeError):
                                        json_metadata = {}
                                    resource_valid = any(
                                        target.get("datasetId") == datasource.id
                                        for fltr in json_metadata.get(
                                            "native_filter_configuration", []
                                        )
                                        for target in fltr.get("targets", [])
                                        if native_filter_id == fltr.get("id")
                                    )
                            else:
                                slice_id = form_data.get("slice_id")
                                if slice_id:
                                    # Chart-in-dashboard validation
                                    slc = await self._get_slice_by_id(slice_id)
                                    if (
                                        slc is not None
                                        and slc in getattr(dashboard_, "slices", [])
                                        and getattr(slc, "datasource", None)
                                        == datasource
                                    ):
                                        resource_valid = True

                                # Drill-by access check
                                if not resource_valid:
                                    resource_valid = await self._has_drill_by_access(
                                        form_data, dashboard_, datasource
                                    )

                            # Finally check dashboard-level access
                            if resource_valid and await self.can_access_dashboard(
                                dashboard_, user=user
                            ):
                                dashboard_fallback = True

                if not dashboard_fallback:
                    raise SupersetSecurityException(
                        self.get_datasource_access_error_object(datasource)
                    )

        # ------------------------------------------------------------------
        # Path 4: dashboard
        # Mirrors original lines 2487-2527.
        # ------------------------------------------------------------------
        if dashboard:
            if self.is_guest_user(user):
                # Guest user is currently used for embedded dashboards only.
                if await self.has_guest_access(dashboard, user=user):
                    return
                raise SupersetSecurityException(
                    self.get_dashboard_access_error_object(dashboard)
                )

            if not self.is_admin(user):
                # ``is_owner`` reads ``dashboard.owners`` synchronously — load
                # it first so a bare-fetched dashboard (e.g. the welcome-page
                # lookup in spa.py) doesn't trip a sync lazy-load
                # (MissingGreenlet) on the async session. Mirrors Path 3.
                await self._ensure_relationship_loaded(dashboard, "owners")
            if self.is_admin(user) or self.is_owner(dashboard, user):
                return

            # DASHBOARD_RBAC logic
            if self._dashboard_rbac_enabled:
                await self._ensure_relationship_loaded(dashboard, "roles")
            if self._dashboard_rbac_enabled and getattr(dashboard, "roles", []):
                if getattr(dashboard, "published", False) and {
                    role.id for role in getattr(dashboard, "roles", [])
                } & {role.id for role in getattr(user, "roles", [])}:
                    return

            # REGULAR RBAC logic
            # User can only access the dashboard in case:
            #    It doesn't have any datasets; OR
            #    They have access to at least one dataset used.
            # Mirrors original lines 2519-2523 ``not dashboard.datasources or any(
            # can_access_datasource(...) for ... in dashboard.datasources)``.
            else:
                # ``Dashboard.datasources`` returns ``None`` (not an empty set)
                # when the instance is bound to an ``AsyncSession`` — synchronous
                # I/O on the sync proxy would raise ``MissingGreenlet``. We must
                # NOT treat ``None`` as "no datasources -> grant"; that bypasses
                # authorization. Distinguish ``None`` (async: derive & check via
                # the same async-safe slice iteration ``can_access_dashboard``
                # uses) from an empty set (genuinely zero datasources -> grant).
                datasources = getattr(dashboard, "datasources", None)
                if datasources is not None:
                    if not datasources:
                        return  # Dashboard with zero datasets is accessible.
                    for ds in datasources:
                        if await self.can_access_datasource(ds, user=user):
                            return
                else:
                    # Async-safe fallback: iterate slices and check each slice's
                    # datasource (mirrors ``can_access_dashboard`` lines 1632-1644).
                    await self._ensure_relationship_loaded(dashboard, "slices")
                    slices = getattr(dashboard, "slices", [])
                    if not slices:
                        return  # No slices -> no datasets -> accessible.
                    for slc in slices:
                        # ``Slice.datasource`` proxies the ``table`` relationship;
                        # pre-load it so the sync access read doesn't trip a
                        # MissingGreenlet on an un-eager-loaded dashboard.
                        await self._ensure_relationship_loaded(slc, "table")
                        datasource = getattr(slc, "datasource", None)
                        if datasource and await self.can_access_datasource(
                            datasource, user=user
                        ):
                            return

            raise SupersetSecurityException(
                self.get_dashboard_access_error_object(dashboard)
            )

        # ------------------------------------------------------------------
        # Path 5: chart
        # Mirrors original lines 2529-2536.
        # ------------------------------------------------------------------
        if chart:
            if self.is_admin(user) or self.is_owner(chart, user):
                return

            chart_ds = getattr(chart, "datasource", None)
            if chart_ds and await self.can_access_datasource(chart_ds, user=user):
                return

            raise SupersetSecurityException(self.get_chart_access_error_object(chart))

    @staticmethod
    def _get_default_schema_for_query(
        database: Any,
        query: Any,
        template_params: dict[str, Any] | None = None,
    ) -> str | None:
        """Return the default schema for a given query.

        Mirrors ``Database.get_default_schema_for_query`` from the original
        which delegates to ``db_engine_spec.get_default_schema_for_query``.

        Since the liteset Database model may not have this method, we
        replicate the logic from ``BaseEngineSpec.get_default_schema_for_query``:

        1. If the engine spec supports dynamic schemas, use the query schema.
        2. Otherwise check if the schema is in the SQLAlchemy URI / connect_args.
        3. Fall back to ``get_default_schema(database, query.catalog)``.
        """
        if not hasattr(database, "db_engine_spec"):
            return getattr(query, "schema", None)

        spec = database.db_engine_spec

        # Original: Database.get_default_schema_for_query delegates to engine spec
        if hasattr(spec, "get_default_schema_for_query"):
            return spec.get_default_schema_for_query(database, query, template_params)

        # Inline the BaseEngineSpec.get_default_schema_for_query logic
        if getattr(spec, "supports_dynamic_schema", False):
            return getattr(query, "schema", None)

        # Check if schema is stored in SQLAlchemy URI or connect_args
        try:
            connect_args = database.get_extra()["engine_params"]["connect_args"]
        except (KeyError, TypeError):
            connect_args = {}

        if hasattr(spec, "get_schema_from_engine_params"):
            from sqlalchemy.engine import make_url as make_url_safe

            sqlalchemy_uri = make_url_safe(database.sqlalchemy_uri)
            schema_from_params = spec.get_schema_from_engine_params(
                sqlalchemy_uri, connect_args
            )
            if schema_from_params:
                return schema_from_params

        # Fall back to default schema for the catalog
        if hasattr(spec, "get_default_schema"):
            return spec.get_default_schema(database, getattr(query, "catalog", None))

        return getattr(query, "schema", None)

    async def _can_access_datasource_schema(
        self, datasource: Any, *, user: Any
    ) -> bool:
        """Check schema-level access for a datasource.

        Mirrors the original ``can_access_schema(datasource)`` which takes
        a datasource and checks all_datasource_access, database, catalog,
        and schema_perm.
        """
        if await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        ):
            return True
        database = getattr(datasource, "database", None)
        if database and await self.can_access_database(database, user=user):
            return True
        ds_catalog = getattr(datasource, "catalog", None)
        if ds_catalog and database:
            if await self.can_access_catalog(database, ds_catalog, user=user):
                return True
        schema_perm = getattr(datasource, "schema_perm", None)
        if schema_perm and await self.can_access(SCHEMA_ACCESS, schema_perm, user=user):
            return True
        return False

    async def _get_dashboard_by_id(self, dashboard_id: Any) -> Any | None:
        """Load a Dashboard by ID for dashboard RBAC fallback.

        Mirrors the original ``self.session.query(Dashboard)
        .filter(Dashboard.id == dashboard_id).one_or_none()``.

        Eager-loads the relationships the fallback then reads synchronously
        (``roles``/``slices`` in ``raise_for_access``, ``owners``/``roles``/
        ``slices`` in ``can_access_dashboard``) — a bare select would trip a
        sync lazy-load → ``MissingGreenlet`` on the async session.
        """
        from superset.models.dashboard import Dashboard

        stmt = (
            select(Dashboard)
            .where(Dashboard.id == dashboard_id)
            .options(
                selectinload(Dashboard.roles),
                selectinload(Dashboard.owners),
                selectinload(Dashboard.slices),
            )
        )
        result = await self.dao.session.execute(stmt)
        return result.scalars().one_or_none()

    async def _get_slice_by_id(self, slice_id: Any) -> Any | None:
        """Load a Slice by ID for chart-in-dashboard validation.

        Eager-loads ``table`` because the caller reads ``slc.datasource``
        (a sync proxy over the ``table`` relationship) right after.
        """
        from superset.models.slice import Slice

        stmt = (
            select(Slice).where(Slice.id == slice_id).options(selectinload(Slice.table))
        )
        result = await self.dao.session.execute(stmt)
        return result.scalars().one_or_none()

    async def _has_drill_by_access(
        self,
        form_data: dict[str, Any],
        dashboard: Any,
        datasource: Any,
    ) -> bool:
        """Check if form_data is performing a supported drill-by operation.

        Mirrors the original ``has_drill_by_access`` exactly:
        - type != NATIVE_FILTER
        - slice_id == 0
        - chart_id must reference a chart in the dashboard
        - chart datasource must match
        - requested dimensions must be a subset of drillable columns
        """
        from superset.models.connectors import TableColumn
        from superset.models.slice import Slice

        if form_data.get("type") == "NATIVE_FILTER":
            return False
        if form_data.get("slice_id") != 0:
            return False
        chart_id = form_data.get("chart_id")
        if not chart_id:
            return False

        # Load the chart (eager ``table`` — ``slc.datasource`` reads it sync)
        stmt = (
            select(Slice).where(Slice.id == chart_id).options(selectinload(Slice.table))
        )
        result = await self.dao.session.execute(stmt)
        slc = result.scalars().one_or_none()
        if slc is None:
            return False
        if slc not in getattr(dashboard, "slices", []):
            return False
        if getattr(slc, "datasource", None) != datasource:
            return False

        dimensions = form_data.get("groupby")
        if not dimensions:
            return False

        # Load drillable columns
        stmt_cols = (
            select(TableColumn.column_name)
            .where(TableColumn.table_id == datasource.id)
            .where(TableColumn.groupby.is_(True))
        )
        result_cols = await self.dao.session.execute(stmt_cols)
        drillable_columns = {row[0] for row in result_cols.all()}
        if not drillable_columns:
            return False

        return set(dimensions).issubset(drillable_columns)

    async def can_access_database(self, database: Any, *, user: Any) -> bool:
        """Check if user can access a database."""
        if self.is_admin(user):
            return True
        if await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        ):
            return True
        if await self.has_access(ALL_DATABASE_ACCESS, ALL_DATABASE_ACCESS, user=user):
            return True
        perm = getattr(database, "perm", None)
        if perm and await self.has_access(DATABASE_ACCESS, perm, user=user):
            return True
        return False

    async def can_access_schema(
        self,
        database: Any,
        schema: str,
        *,
        catalog: str | None = None,
        user: Any,
    ) -> bool:
        """Check if user can access a specific schema.

        Mirrors the original ``can_access_schema(datasource)`` hierarchy:
        all_datasource_access -> database_access -> catalog_access -> schema_access.

        For catalog-aware databases (e.g. ClickHouse, Trino), pass the
        ``catalog`` parameter to build the 3-part permission string
        ``[db].[catalog].[schema]``.  Without a catalog the traditional
        2-part ``[db].[schema]`` is used.
        """
        if await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        ):
            return True
        if await self.can_access_database(database, user=user):
            return True
        # Catalog-level check — mirrors original line 555-557
        if catalog:
            if await self.can_access_catalog(database, catalog, user=user):
                return True
        db_name = getattr(database, "database_name", "")
        if catalog:
            schema_perm = f"[{db_name}].[{catalog}].[{schema}]"
        else:
            schema_perm = f"[{db_name}].[{schema}]"
        return await self.has_access(SCHEMA_ACCESS, schema_perm, user=user)

    async def can_access_table(
        self,
        database: Any,
        table: Any,
        *,
        user: Any,
    ) -> bool:
        """Check if user can access a specific table.

        Mirrors ``SupersetSecurityManager.can_access_table`` from the
        original security manager.

        :param database: The Database model instance
        :param table: A ``Table`` instance with ``.table``, ``.schema``,
            ``.catalog`` attributes
        :param user: The current user
        :returns: Whether the user can access the table
        """
        try:
            await self.raise_for_access(database=database, table=table, user=user)
        except SupersetSecurityException:
            return False
        return True

    async def _can_access_table_datasource(
        self,
        database: Any,
        table_name: str,
        schema: str | None,
        catalog: str | None,
        *,
        user: Any,
    ) -> bool:
        """Check datasource-level access for a specific table.

        Looks up SqlaTable rows matching the given table name and checks
        if the user has ``datasource_access`` or is owner of any matching
        datasource.  Mirrors the original table-level access check
        where individual datasource permissions are checked after
        database/catalog/schema checks fail.
        """
        try:
            from superset.models.connectors import SqlaTable

            session = self.dao.session
            stmt = select(SqlaTable).where(
                SqlaTable.table_name == table_name,
                SqlaTable.database_id == database.id,
            )
            if schema is not None:
                stmt = stmt.where(SqlaTable.schema == schema)
            if catalog is not None and hasattr(SqlaTable, "catalog"):
                stmt = stmt.where(SqlaTable.catalog == catalog)

            result = await session.execute(stmt)
            datasources = result.scalars().all()

            for ds in datasources:
                if await self.can_access_datasource(ds, user=user):
                    return True
                if self.is_owner(ds, user):
                    return True
        except (SQLAlchemyError, NoResultFound):
            logger.warning(
                "Failed to check table datasource access for %s.%s",
                schema,
                table_name,
                exc_info=True,
            )
        return False

    async def can_access_datasource(self, datasource: Any, *, user: Any) -> bool:
        """Check if user can access a datasource.

        Note: The original ``can_access_datasource`` delegates to
        ``raise_for_access(datasource=datasource)`` which checks
        schema access, datasource_access perm, AND ownership.
        We inline those checks here rather than recursing into
        raise_for_access to avoid the dashboard RBAC fallback path.
        """
        if self.is_admin(user):
            return True
        if await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        ):
            return True
        perm = getattr(datasource, "perm", None)
        if perm and await self.has_access(DATASOURCE_ACCESS, perm, user=user):
            return True
        # Pre-load the relationships the ownership + schema checks below read
        # synchronously (``owners``, ``database``) so a bare-fetched datasource
        # doesn't trip a MissingGreenlet on the AsyncSession.
        await self._ensure_relationship_loaded(datasource, "owners")
        await self._ensure_relationship_loaded(datasource, "database")
        # Ownership check — mirrors original raise_for_access line 2429
        if self.is_owner(datasource, user):
            return True
        # Schema-level check (includes database, catalog, schema_perm)
        if await self._can_access_datasource_schema(datasource, user=user):
            return True
        return False

    async def can_access_dashboard(self, dashboard: Any, *, user: Any) -> bool:  # noqa: C901
        """Check if user can access a dashboard."""
        if self.is_admin(user):
            return True

        if self.is_guest_user(user):
            return await self.has_guest_access(dashboard, user=user)

        # ``is_owner``/``roles`` are read synchronously — pre-load so a
        # bare-fetched dashboard doesn't trip MissingGreenlet (no-op for
        # callers that already eager-loaded).
        await self._ensure_relationship_loaded(dashboard, "owners")
        if self.is_owner(dashboard, user):
            return True

        await self._ensure_relationship_loaded(dashboard, "roles")
        dashboard_roles = getattr(dashboard, "roles", [])
        if self._dashboard_rbac_enabled and dashboard_roles:
            if not getattr(dashboard, "published", False):
                return False
            user_role_ids = {r.id for r in getattr(user, "roles", [])}
            dashboard_role_ids = {r.id for r in dashboard_roles}
            return bool(user_role_ids & dashboard_role_ids)

        # Non-RBAC: check datasource-based access
        # Prefer dashboard.datasources (M2M property) over iterating slices
        datasources = getattr(dashboard, "datasources", None)
        if datasources is not None:
            if not datasources:
                return True  # Empty dashboard is accessible to all authenticated users
            for ds in datasources:
                if await self.can_access_datasource(ds, user=user):
                    return True
            return False
        # Fallback: iterate slices
        await self._ensure_relationship_loaded(dashboard, "slices")
        slices = getattr(dashboard, "slices", [])
        if not slices:
            return True
        for slc in slices:
            # ``Slice.datasource`` proxies the ``table`` relationship; pre-load
            # it so the sync access read doesn't trip a MissingGreenlet when the
            # dashboard was fetched without eager-loading slice datasources.
            await self._ensure_relationship_loaded(slc, "table")
            datasource = getattr(slc, "datasource", None)
            if datasource and await self.can_access_datasource(datasource, user=user):
                return True
        return False

    def is_owner(self, resource: Any, user: Any) -> bool:
        """Check if user is an owner of the resource (owners M2M only).

        Mirrors ``raise_for_ownership`` in the original upstream
        SecurityManager: admins are deemed owners of every resource and
        skip the ``owners`` check entirely.

        The ``owners`` collection must be eagerly loaded by the caller
        (e.g. via ``selectinload(Slice.owners)``) — accessing the
        relationship attribute on an async session without preload
        triggers a sync lazy-load that fails with ``MissingGreenlet``.
        """
        if self.is_admin(user):
            return True
        user_id: int | None
        if isinstance(user, int):
            user_id = user
        else:
            user_id = getattr(user, "id", None)
        if user_id is None:
            return False
        owners = getattr(resource, "owners", [])
        return any(getattr(o, "id", None) == user_id for o in owners)

    @staticmethod
    async def _ensure_relationship_loaded(resource: Any, attr: str) -> None:
        """Async-load a lazy relationship before a *synchronous* access check.

        The sync ``is_owner`` / ``can_access_datasource`` helpers walk ORM
        relationships (``owners``, ``database``).  When the caller fetched the
        resource without eager-loading them (a bare ``dao.find_by_id``),
        touching the attribute on an object bound to an ``AsyncSession`` emits a
        sync lazy-load → ``MissingGreenlet`` → HTTP 500.  This pre-loads the
        attribute through the object's own async session so the subsequent sync
        read is a no-op DB-wise.

        Safe no-op when ``resource`` is not an ORM instance (MagicMock/dict),
        the model has no such relationship (``Query``/``Database`` have no
        ``owners``), the attribute is already loaded, or the object is detached.
        """
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.ext.asyncio import async_object_session

        state = sa_inspect(resource, raiseerr=False)
        if state is None:
            return
        if attr not in state.mapper.relationships:
            return
        if attr not in state.unloaded:
            return
        session = async_object_session(resource)
        if session is None:
            return
        try:
            await session.refresh(resource, attribute_names=[attr])
        except Exception:  # noqa: BLE001 — best-effort; sync read falls back
            logger.debug("Could not pre-load %s for access check", attr, exc_info=True)

    async def get_schemas_accessible_by_user(
        self,
        database: Any,
        schemas: list[str],
        *,
        catalog: str | None = None,
        hierarchical: bool = True,
        user: Any,
    ) -> list[str]:
        """Filter schemas to only those accessible by the user.

        Mirrors original ``get_schemas_accessible_by_user``
        (superset_old/security/manager.py:895-964).

        If no catalog is specified, the default catalog is used.

        :param database: The SQL database
        :param schemas: Candidate schemas
        :param catalog: An optional database catalog
        :param hierarchical: Whether to check using hierarchical permission logic
        :param user: The current user
        :returns: The list of accessible database schemas
        """
        from superset.models.connectors import SqlaTable

        default_catalog = (
            database.get_default_catalog()
            if hasattr(database, "get_default_catalog")
            else None
        )
        catalog = catalog or default_catalog

        # Hierarchical shortcut: database-level or catalog-level access
        # grants access to all schemas within.  The admin check is folded
        # into can_access_database / can_access_catalog, so it is only
        # applied when hierarchical=True — matching the original
        # superset_old/security/manager.py:920-924 behaviour.
        if hierarchical:
            if await self.can_access_database(database, user=user):
                return schemas
            if catalog and await self.can_access_catalog(database, catalog, user=user):
                return schemas

        # schema_access — parse 2-part and 3-part perm strings
        accessible_schemas: set[str] = set()
        db_name = getattr(database, "database_name", "")
        user_perms = await self._user_permission_pairs(user)

        schema_access_perms = {
            view_name
            for perm_name, view_name in user_perms
            if perm_name == SCHEMA_ACCESS
        }
        default_schema = (
            database.get_default_schema(default_catalog)
            if hasattr(database, "get_default_schema")
            else None
        )

        for perm in schema_access_perms:
            parts = [part[1:-1] for part in perm.split(".")]

            if parts[0] != db_name:
                continue

            # [database].[schema] matches when no catalog is specified, or when
            # the user specifies the default catalog
            if len(parts) == 2 and (catalog is None or catalog == default_catalog):
                accessible_schemas.add(parts[1])

            # [database].[catalog].[schema] matches when the catalog is equal to
            # the requested catalog or, when no catalog specified, it's equal to
            # the default catalog.
            elif len(parts) == 3 and parts[1] == catalog:
                accessible_schemas.add(parts[2])

        # datasource_access — infer schema access from accessible datasources
        datasource_access_perms = {
            view_name
            for perm_name, view_name in user_perms
            if perm_name == DATASOURCE_ACCESS
        }
        if datasource_access_perms:
            stmt = (
                select(SqlaTable.schema)
                .where(SqlaTable.database_id == database.id)
                .where(SqlaTable.perm.in_(datasource_access_perms))
                .distinct()
            )
            result = await self.dao.session.execute(stmt)
            accessible_schemas.update(
                {
                    str(row[0] or default_schema)
                    for row in result
                    if (row[0] or default_schema)
                }
            )

        schemas_set = set(schemas)
        return [s for s in schemas if s in (schemas_set & accessible_schemas)]

    async def get_datasources_accessible_by_user(self, *, user: Any) -> list[str]:
        """Get datasource perm strings the user can access.

        Returns perm strings (e.g. "[db].[schema].[table]"), not ORM objects.
        Controllers in superset/core-api will use these to filter querysets.
        """
        if self.is_admin(user):
            return []  # Admin can access all — empty means no filter
        user_perms = await self._user_permission_pairs(user)
        return [
            view_name
            for perm_name, view_name in user_perms
            if perm_name == DATASOURCE_ACCESS
        ]

    async def filter_datasources_by_perms(
        self,
        *,
        database: Any,
        datasource_names: list[Any],
        catalog: str | None = None,
        schema: str | None = None,
        user: Any,
    ) -> list[Any]:
        """Filter ``DatasourceName`` tuples to those accessible by the user.

        Async port of
        ``superset_old/security/manager.py::SupersetSecurityManager
        .get_datasources_accessible_by_user`` (line 1026). When ``catalog``
        and/or ``schema`` are specified, every datasource in
        ``datasource_names`` is assumed to live in that catalog/schema.

        Access short-circuits (return ALL) on, in order:

        * ``can_access_database`` (or admin / ``all_datasource_access``);
        * ``catalog_access`` on the resolved catalog;
        * ``schema_access`` on the (catalog, schema) pair.

        Otherwise the user's individually-granted ``datasource_access`` /
        ``catalog_access`` / ``schema_access`` view-menus are resolved and the
        physical ``SqlaTable`` rows whose ``perm`` / ``schema_perm`` /
        ``catalog_perm`` match are loaded; the input list is intersected with
        those names — exactly mirroring the original's
        ``SqlaTable.query_datasources_by_permissions`` branch (which the
        previous port deliberately under-exposed).
        """
        if await self.can_access_database(database, user=user):
            return datasource_names

        catalog = catalog or database.get_default_catalog()
        if catalog:
            catalog_perm = self.get_catalog_perm(database.database_name, catalog)
            if catalog_perm and await self.can_access(
                CATALOG_ACCESS, catalog_perm, user=user
            ):
                return datasource_names

        if schema:
            schema_perm = self.get_schema_perm(
                database.database_name,
                schema,
                catalog=catalog,
            )
            if schema_perm and await self.can_access(
                SCHEMA_ACCESS, schema_perm, user=user
            ):
                return datasource_names

        # No blanket grant — fall back to per-table ``datasource_access`` (plus
        # any catalog/schema-scoped) view-menus and intersect with the inputs.
        user_perms = await self.user_view_menu_names(DATASOURCE_ACCESS, user=user)
        catalog_perms = await self.user_view_menu_names(CATALOG_ACCESS, user=user)
        schema_perms = await self.user_view_menu_names(SCHEMA_ACCESS, user=user)

        from sqlalchemy import or_, select

        from superset.models.connectors import SqlaTable
        from superset.utils.core import DatasourceName

        filters = [
            column.in_(perms)
            for column, perms in (
                (SqlaTable.perm, user_perms),
                (SqlaTable.schema_perm, schema_perms),
                (SqlaTable.catalog_perm, catalog_perms),
            )
            if perms
        ]
        if not filters:
            return []

        stmt = (
            select(SqlaTable)
            .where(SqlaTable.database_id == database.id)
            .where(or_(*filters))
        )
        result = await self.dao.session.execute(stmt)
        user_datasources = {
            DatasourceName(
                cast("str", t.table_name),
                cast("str", t.schema),
                cast("str | None", t.catalog),
            )
            for t in result.scalars().all()
        }

        return [
            datasource
            for datasource in datasource_names
            if datasource in user_datasources
        ]

    async def _resolve_user_roles_for_rls(self, user: Any) -> list[Any] | None:
        """Resolve the list of role objects to use for RLS filtering.

        Mirrors the original ``SupersetSecurityManager.get_user_roles``
        contract used inside ``get_rls_filters``:

        * Authenticated user → their assigned roles.
        * Anonymous / missing user → ``[Public role]`` if
          ``AUTH_ROLE_PUBLIC`` is configured *and* the role exists in
          the metadata DB; otherwise ``None`` to signal "no roles, no
          filtering — return [] from ``get_rls_filters``".
        """
        # ``user is None`` → skip RLS entirely (upstream returns [] before
        # touching roles). Distinct from an *anonymous* user, which proceeds
        # with an (often empty) role list so BASE filters still apply.
        if user is None:
            return None

        is_anonymous = bool(getattr(user, "is_anonymous", False)) or not getattr(
            user, "is_authenticated", True
        )
        if not is_anonymous:
            # Include group-inherited roles (dao.get_user_roles expands
            # ab_user_group -> ab_group_role) — 1:1 with upstream
            # ``get_rls_filters`` which uses upstream ``get_user_roles`` (direct +
            # group roles).  Without the group roles, a REGULAR RLS filter
            # scoped to a group-assigned role never matches and the row
            # restriction is silently skipped (data exposure).
            return await self.dao.get_user_roles(user)

        public_role_name = self._public_role_name
        if not public_role_name:
            return []
        public_role = await self.dao.get_role_by_name(public_role_name)
        if public_role is None:
            return []
        return [public_role]

    async def get_rls_filters(self, table: Any, *, user: Any) -> list[Any]:
        """Retrieve RLS filters for the current user and table.

        Ported 1:1 from
        ``superset_old/security/manager.py::SupersetSecurityManager.get_rls_filters``.

        Two filter types:
        - **Regular**: applies if the user holds one of the listed roles.
        - **Base**: applies if the user does *not* hold one of the listed
          roles (Admin is exempted by listing the Admin role on the
          BASE filter, exactly as in the original — there is no
          special-cased ``is_admin`` branch).

        Anonymous users get the Public role (if ``AUTH_ROLE_PUBLIC`` is
        configured), exactly mirroring the original
        ``SupersetSecurityManager.get_user_roles`` fallback. If no Public
        role can be resolved, returns ``[]``.
        """
        from superset.models.connectors import (
            RLSFilterRoles,
            RLSFilterTables,
            RowLevelSecurityFilter,
        )
        from superset.utils.core import RowLevelSecurityFilterType

        roles = await self._resolve_user_roles_for_rls(user)
        if roles is None:
            return []
        user_roles = [r.id for r in roles]

        filter_tables_sq = select(RLSFilterTables.c.rls_filter_id).where(
            RLSFilterTables.c.table_id == table.id
        )

        regular_filter_roles_sq = (
            select(RLSFilterRoles.c.rls_filter_id)
            .join(
                RowLevelSecurityFilter,
                RLSFilterRoles.c.rls_filter_id == RowLevelSecurityFilter.id,
            )
            .where(
                RowLevelSecurityFilter.filter_type == RowLevelSecurityFilterType.REGULAR
            )
            .where(RLSFilterRoles.c.role_id.in_(user_roles))
        )

        base_filter_roles_sq = (
            select(RLSFilterRoles.c.rls_filter_id)
            .join(
                RowLevelSecurityFilter,
                RLSFilterRoles.c.rls_filter_id == RowLevelSecurityFilter.id,
            )
            .where(
                RowLevelSecurityFilter.filter_type == RowLevelSecurityFilterType.BASE
            )
            .where(RLSFilterRoles.c.role_id.in_(user_roles))
        )

        stmt = (
            select(
                RowLevelSecurityFilter.id,
                RowLevelSecurityFilter.group_key,
                RowLevelSecurityFilter.clause,
            )
            .where(RowLevelSecurityFilter.id.in_(filter_tables_sq))
            .where(
                or_(
                    and_(
                        RowLevelSecurityFilter.filter_type
                        == RowLevelSecurityFilterType.REGULAR,
                        RowLevelSecurityFilter.id.in_(regular_filter_roles_sq),
                    ),
                    and_(
                        RowLevelSecurityFilter.filter_type
                        == RowLevelSecurityFilterType.BASE,
                        RowLevelSecurityFilter.id.notin_(base_filter_roles_sq),
                    ),
                )
            )
        )

        # Mirror the original which returns ``[(id, group_key, clause), ...]``
        # via ``self.session.query(RLSF.id, RLSF.group_key, RLSF.clause)``.
        # Row objects support ``.id``/``.group_key``/``.clause`` attribute
        # access exactly like ORM instances.
        result = await self.dao.session.execute(stmt)
        return list(result.all())

    async def get_rls_sorted(self, table: Any, *, user: Any) -> list[Any]:
        """Retrieve RLS filters sorted by ID for deterministic cache keys.

        :param table: The datasource/table to check against.
        :param user: The current user.
        :returns: A list of RowLevelSecurityFilter objects sorted by ID.
        """
        filters = await self.get_rls_filters(table, user=user)
        filters.sort(key=lambda f: f.id)
        return filters

    def get_guest_rls_filters(self, dataset: Any, *, user: Any) -> list[dict[str, Any]]:
        """Retrieve RLS filters from a guest token for the given dataset.

        Matches the original SupersetSecurityManager.get_guest_rls_filters:
        returns rules from the guest token that either have no dataset
        restriction or match the given dataset's ID.

        :param dataset: The datasource to check against.
        :param user: The current user (may be a GuestUser with rls_rules).
        :returns: A list of RLS rule dicts from the guest token.
        """
        if not self.is_guest_user(user):
            return []
        rls_rules: list[dict[str, Any]] = getattr(user, "rls_rules", [])
        return [
            rule
            for rule in rls_rules
            if not rule.get("dataset") or str(rule.get("dataset")) == str(dataset.id)
        ]

    def get_guest_rls_filters_str(self, table: Any, *, user: Any) -> list[str]:
        """Return guest RLS filter clauses as strings.

        :param table: The datasource to check against.
        :param user: The current user.
        :returns: A list of clause strings from guest token RLS rules.
        """
        return [
            f.get("clause", "") for f in self.get_guest_rls_filters(table, user=user)
        ]

    async def get_rls_cache_key(self, datasource: Any, *, user: Any) -> list[str]:
        """Return cache key components representing active RLS filters.

        Combines both regular RLS filters (from DB, sorted by ID) and
        guest token RLS filters to build a deterministic list of strings
        for cache differentiation. This matches the original
        SupersetSecurityManager.get_rls_cache_key exactly.
        """
        rls_clauses_with_group_key: list[str] = []
        if getattr(datasource, "is_rls_supported", False):
            rls_clauses_with_group_key = [
                f"{f.clause}-{f.group_key or ''}"
                for f in await self.get_rls_sorted(datasource, user=user)
            ]
        guest_rls = self.get_guest_rls_filters_str(datasource, user=user)
        return guest_rls + rls_clauses_with_group_key

    async def invalidate_user_cache(self, redis: "Redis[Any]", user: Any) -> None:
        """Invalidate Redis auth cache for a user.

        Deletes all possible cache keys: by id, username, and email.
        This ensures cache is fully cleared regardless of which key
        was used to store the cached user data.
        """
        keys = [f"auth:user:{user.id}"]
        username = getattr(user, "username", None)
        if username:
            keys.append(f"auth:user:{username}")
        email = getattr(user, "email", None)
        if email:
            keys.append(f"auth:user:{email}")
        await redis.delete(*keys)

    # --- Permission string formatters ---

    @staticmethod
    def get_database_perm(database_name: str, database_id: int) -> str:
        """Format database permission string: [db_name].(id:123)."""
        return f"[{database_name}].(id:{database_id})"

    @staticmethod
    def get_schema_perm(
        database: Any,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> str | None:
        """Format schema permission string.

        Returns ``None`` when ``schema`` is ``None`` (1:1 with original
        ``superset_old/security/manager.py:431``), otherwise
        ``[db].[catalog].[schema]`` or ``[db].[schema]``.
        """
        if schema is None:
            return None

        # 1:1 with the original: ``raise_for_access`` passes the Database
        # OBJECT here, so ``str(database)`` resolves to
        # ``Database.__repr__`` → ``name`` (``verbose_name or database_name``),
        # while the PVM-creation callers (sync_permissions / permission_manager)
        # pass a ``database_name`` *string* — ``str`` of which is the string
        # itself. This split exactly mirrors upstream (object on the access
        # check, name string on creation).
        db_name = str(database)
        if catalog:
            return f"[{db_name}].[{catalog}].[{schema}]"
        return f"[{db_name}].[{schema}]"

    @staticmethod
    def get_dataset_perm(database_name: str, dataset_name: str, dataset_id: int) -> str:
        """Format dataset permission string: [db_name].[dataset_name](id:N)."""
        return f"[{database_name}].[{dataset_name}](id:{dataset_id})"

    @staticmethod
    def get_catalog_perm(
        database_name: str,
        catalog: str | None = None,
    ) -> str | None:
        """Format catalog permission string: [db_name].[catalog].

        Returns ``None`` when ``catalog`` is ``None`` (1:1 with original
        ``superset_old/security/manager.py:414``).
        """
        if catalog is None:
            return None
        return f"[{database_name}].[{catalog}]"

    # --- Permission / view-menu / permission-view CRUD helpers ---
    #
    # Direct async ports of the corresponding methods on the upstream
    # ``security.sqla.manager.SecurityManager``. They are
    # used by ``SyncPermissionsCommand`` (and any future code path that needs
    # to materialise permission rows) to look up or create the
    # ``ab_permission`` / ``ab_view_menu`` / ``ab_permission_view`` rows that
    # the original SecurityManager would have created via SQLAlchemy
    # session.query(...) — the AsyncSession layer cannot run those sync
    # queries safely so we re-implement them here.

    async def find_permission(self, name: str) -> Any | None:
        """Find a row in ``ab_permission`` by name. Mirrors ``find_permission``."""
        from sqlalchemy import select

        from superset.models.security import Permission

        stmt = select(Permission).where(Permission.name == name)
        result = await self.dao.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_view_menu(self, name: str) -> Any | None:
        """Find a row in ``ab_view_menu`` by name. Mirrors ``find_view_menu``."""
        from sqlalchemy import select

        from superset.models.security import ViewMenu

        stmt = select(ViewMenu).where(ViewMenu.name == name)
        result = await self.dao.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_permission_view_menu(
        self, permission_name: str, view_menu_name: str
    ) -> Any | None:
        """Find a row in ``ab_permission_view`` for the given (perm, view_menu) pair.

        Direct port of the upstream ``find_permission_view_menu``.
        """
        from sqlalchemy import select

        from superset.models.security import PermissionView

        permission = await self.find_permission(permission_name)
        view_menu = await self.find_view_menu(view_menu_name)
        if not (permission and view_menu):
            return None
        stmt = select(PermissionView).where(
            PermissionView.permission_id == permission.id,
            PermissionView.view_menu_id == view_menu.id,
        )
        result = await self.dao.session.execute(stmt)
        return result.scalars().one_or_none()

    async def add_permission(self, name: str) -> Any | None:
        """Insert a row into ``ab_permission`` if missing.

        Direct port of the upstream ``add_permission``. Returns the (possibly new)
        :class:`Permission` instance, or ``None`` if creation fails.
        """
        from superset.models.security import Permission

        perm = await self.find_permission(name)
        if perm is not None:
            return perm
        try:
            perm = Permission(name=name)
            self.dao.session.add(perm)
            await self.dao.session.flush()
            return perm
        except Exception:  # noqa: BLE001
            logger.exception("Failed to add permission %s", name)
            return None

    async def add_view_menu(self, name: str) -> Any | None:
        """Insert a row into ``ab_view_menu`` if missing.

        Direct port of the upstream ``add_view_menu``.
        """
        from superset.models.security import ViewMenu

        vm = await self.find_view_menu(name)
        if vm is not None:
            return vm
        try:
            vm = ViewMenu(name=name)
            self.dao.session.add(vm)
            await self.dao.session.flush()
            return vm
        except Exception:  # noqa: BLE001
            logger.exception("Failed to add view_menu %s", name)
            return None

    async def add_permission_view_menu(
        self, permission_name: str, view_menu_name: str
    ) -> Any | None:
        """Insert a row into ``ab_permission_view`` if missing.

        Direct port of the upstream ``add_permission_view_menu``: idempotently
        creates the permission, view-menu and the join row.
        """
        from superset.models.security import PermissionView

        if not (permission_name and view_menu_name):
            return None
        existing = await self.find_permission_view_menu(permission_name, view_menu_name)
        if existing is not None:
            return existing
        vm = await self.add_view_menu(view_menu_name)
        perm = await self.add_permission(permission_name)
        if vm is None or perm is None:
            return None
        try:
            pv = PermissionView(permission_id=perm.id, view_menu_id=vm.id)
            self.dao.session.add(pv)
            await self.dao.session.flush()
            return pv
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to add permission_view (%s, %s)",
                permission_name,
                view_menu_name,
            )
            return None

    # --- Bulk access checks ---

    async def can_access_all_databases(self, *, user: Any) -> bool:
        """Check if user has the all_database_access permission."""
        return await self.has_access(
            ALL_DATABASE_ACCESS, ALL_DATABASE_ACCESS, user=user
        )

    async def can_access_all_datasources(self, *, user: Any) -> bool:
        """Check if user can access all datasources.

        1:1 with ``superset_old/security/manager.py::can_access_all_datasources``
        (line 498): ``all_database_access OR all_datasource_access``. The port
        previously only checked ``all_datasource_access``, so a user granted
        the broader ``all_database_access`` (e.g. the stock Alpha role, which
        has BOTH but the dataset-list filter keyed off this method) was wrongly
        treated as having no global access.
        """
        if await self.can_access_all_databases(user=user):
            return True
        return await self.has_access(
            ALL_DATASOURCE_ACCESS, ALL_DATASOURCE_ACCESS, user=user
        )

    async def can_access_all_queries(self, *, user: Any) -> bool:
        """Check if user has the all_query_access permission."""
        return await self.has_access(ALL_QUERY_ACCESS, ALL_QUERY_ACCESS, user=user)

    async def _user_permission_pairs(self, user: Any) -> Any:
        """``(perm_name, view_name)`` pairs for the user.

        Anonymous users (``UnauthenticatedUser``, id=0) resolve to the
        Public role's permissions — the per-user query would key on id 0
        and return nothing, silently dropping Public-role
        schema/catalog/datasource access (upstream reads the Public role
        via ``get_public_role()``).  Same guard as ``user_view_menu_names``.
        """
        if getattr(user, "is_anonymous", False) or not getattr(user, "id", 0):
            public_role_name = self._public_role_name
            if not public_role_name:
                return []
            public_role = await self.dao.get_role_by_name(public_role_name)
            if public_role is None:
                return []
            pvs = await self.dao.get_role_permissions(public_role.id)
            return [
                (pv.permission.name, pv.view_menu.name)
                for pv in pvs
                if pv.permission is not None and pv.view_menu is not None
            ]
        return await self.dao.get_all_permissions_for_user_with_groups(user.id)

    # --- List-filtering methods (ID-based, for object-level filters) ---

    async def get_accessible_datasource_ids(self, user: Any) -> list[int]:
        """Return list of datasource IDs the user can access.

        Admins get an empty list (meaning no filter — access everything).
        For other users, parses DATASOURCE_ACCESS permission strings using
        the ``[db].[table](id:N)`` regex to extract integer IDs.
        """
        if self.is_admin(user):
            return []
        user_perms = await self._user_permission_pairs(user)
        ids: list[int] = []
        for perm_name, view_name in user_perms:
            if perm_name != DATASOURCE_ACCESS:
                continue
            m = _DATASOURCE_PERM_RE.match(view_name)
            if m:
                ids.append(int(m.group("id")))
        return ids

    async def get_accessible_database_ids(self, user: Any) -> list[int]:
        """Return list of database IDs the user can access.

        Admins get an empty list (meaning no filter — access everything).
        For other users, parses DATABASE_ACCESS permission strings using
        the ``[db].(id:N)`` regex to extract integer IDs.
        """
        if self.is_admin(user):
            return []
        user_perms = await self._user_permission_pairs(user)
        ids: list[int] = []
        for perm_name, view_name in user_perms:
            if perm_name != DATABASE_ACCESS:
                continue
            m = _DATABASE_PERM_RE.match(view_name)
            if m:
                ids.append(int(m.group("id")))
        return ids

    # --- List-filtering methods (perm-string-based) ---

    async def get_accessible_databases(self, *, user: Any) -> list[str]:
        """Get database perm strings the user can access.

        Returns perm strings (e.g. "[db_name].(id:123)"), not ORM objects.
        Controllers in superset/core-api will use these to filter querysets.
        """
        if self.is_admin(user):
            return []
        user_perms = await self._user_permission_pairs(user)
        return [
            view_name
            for perm_name, view_name in user_perms
            if perm_name == DATABASE_ACCESS
        ]

    async def get_catalogs_accessible_by_user(
        self,
        database: Any,
        catalogs: list[str],
        *,
        hierarchical: bool = True,
        user: Any,
    ) -> list[str]:
        """Filter catalogs to only those accessible by the user.

        Mirrors original ``get_catalogs_accessible_by_user``
        (superset_old/security/manager.py:966-1024).

        :param database: The SQL database
        :param catalogs: Candidate catalogs
        :param hierarchical: Whether to check using hierarchical permission logic
        :param user: The current user
        :returns: The list of accessible database catalogs
        """
        from superset.models.connectors import SqlaTable

        if hierarchical and await self.can_access_database(database, user=user):
            return catalogs

        # catalog_access
        accessible_catalogs: set[str] = set()
        db_name = getattr(database, "database_name", "")
        default_catalog = (
            database.get_default_catalog()
            if hasattr(database, "get_default_catalog")
            else None
        )
        user_perms = await self._user_permission_pairs(user)

        catalog_access_perms = {
            view_name
            for perm_name, view_name in user_perms
            if perm_name == CATALOG_ACCESS
        }
        for perm in catalog_access_perms:
            parts = [part[1:-1] for part in perm.split(".")]
            if parts[0] == db_name and len(parts) >= 2:
                accessible_catalogs.add(parts[1])

        # schema_access — infer catalog from schema perm strings
        schema_access_perms = {
            view_name
            for perm_name, view_name in user_perms
            if perm_name == SCHEMA_ACCESS
        }
        for perm in schema_access_perms:
            parts = [part[1:-1] for part in perm.split(".")]

            if parts[0] != db_name:
                continue
            if len(parts) == 2 and default_catalog:
                accessible_catalogs.add(default_catalog)
            elif len(parts) == 3:
                accessible_catalogs.add(parts[1])

        # datasource_access — infer catalog from accessible datasources
        datasource_access_perms = {
            view_name
            for perm_name, view_name in user_perms
            if perm_name == DATASOURCE_ACCESS
        }
        if datasource_access_perms:
            stmt = (
                select(SqlaTable.catalog)
                .where(SqlaTable.database_id == database.id)
                .where(SqlaTable.perm.in_(datasource_access_perms))
                .distinct()
            )
            result = await self.dao.session.execute(stmt)
            accessible_catalogs.update(
                {
                    str(row[0] or default_catalog)
                    for row in result
                    if (row[0] or default_catalog)
                }
            )

        catalogs_set = set(catalogs)
        return [c for c in catalogs if c in (catalogs_set & accessible_catalogs)]

    async def user_view_menu_names(
        self, permission_name: str, *, user: Any
    ) -> set[str]:
        """Get all view_menu names a user has for a given permission.

        1:1 with ``superset_old/security/manager.py:841``: queries
        view-menu names for the user's roles (and groups). Does NOT
        short-circuit for admins — the original returns the Admin role's
        actual view-menu names, callers decide how to use them.
        """
        # Anonymous user → use the Public role's view-menus, 1:1 with upstream
        # ``if public_role := self.get_public_role(): ...``. The per-user query
        # would key on ``user.id`` (0 for ``UnauthenticatedUser``) and return
        # nothing, silently dropping Public-role schema/datasource access.
        if getattr(user, "is_anonymous", False) or not getattr(user, "id", 0):
            public_role_name = self._public_role_name
            if not public_role_name:
                return set()
            public_role = await self.dao.get_role_by_name(public_role_name)
            if public_role is None:
                return set()
            pvs = await self.dao.get_role_permissions(public_role.id)
            return {
                pv.view_menu.name
                for pv in pvs
                if getattr(pv.permission, "name", None) == permission_name
            }

        user_perms = await self._user_permission_pairs(user)
        return {
            view_name
            for perm_name, view_name in user_perms
            if perm_name == permission_name
        }

    # --- Error object methods ---
    # These return SupersetError dataclasses, matching the original 1:1.

    @staticmethod
    def get_datasource_access_error_msg(datasource: Any) -> str:
        """Return the error message for the denied datasource."""
        ds_id = getattr(datasource, "id", "")
        return (
            f"This endpoint requires the datasource {ds_id}, "
            "database or `all_datasource_access` permission"
        )

    def get_datasource_access_link(self, datasource: Any) -> str | None:
        """Return the link for the denied datasource.

        Reads ``PERMISSION_INSTRUCTIONS_LINK`` (``permission_instructions_link``
        in settings) at call time — mirrors the original
        ``SupersetSecurityManager.get_datasource_access_link`` which calls
        ``get_conf().get("PERMISSION_INSTRUCTIONS_LINK")`` on every invocation
        rather than caching the value at construction time.
        """
        link = getattr(self._settings, "permission_instructions_link", "") or ""
        return link or None

    def get_datasource_access_error_object(
        self,
        datasource: Any,
    ) -> SupersetError:
        """Return the SupersetError for the denied datasource."""
        return SupersetError(
            error_type=SupersetErrorType.DATASOURCE_SECURITY_ACCESS_ERROR,
            message=self.get_datasource_access_error_msg(datasource),
            level=ErrorLevel.WARNING,
            extra={
                "link": self.get_datasource_access_link(datasource),
                "datasource": getattr(datasource, "id", ""),
                "datasource_name": getattr(datasource, "name", ""),
            },
        )

    @staticmethod
    def get_dashboard_access_error_object(
        dashboard: Any,
    ) -> SupersetError:
        """Return the SupersetError for the denied dashboard."""
        return SupersetError(
            error_type=SupersetErrorType.DASHBOARD_SECURITY_ACCESS_ERROR,
            message="You don't have access to this dashboard.",
            level=ErrorLevel.WARNING,
        )

    @staticmethod
    def get_chart_access_error_object(
        chart: Any,
    ) -> SupersetError:
        """Return the SupersetError for the denied chart."""
        return SupersetError(
            error_type=SupersetErrorType.CHART_SECURITY_ACCESS_ERROR,
            message="You don't have access to this chart.",
            level=ErrorLevel.WARNING,
        )

    def get_table_access_error_msg(self, tables: set[Any]) -> str:
        """Return the error message for the denied SQL tables."""
        quoted_tables = [f"`{table}`" for table in tables]
        return (
            f"You need access to the following tables: {', '.join(quoted_tables)},\n"
            "            `all_database_access` or `all_datasource_access` permission"
        )

    def get_table_access_link(self, tables: set[Any]) -> str | None:
        """Return the access link for the denied SQL tables.

        Reads ``PERMISSION_INSTRUCTIONS_LINK`` at call time — mirrors the
        original ``get_table_access_link`` which calls
        ``get_conf().get("PERMISSION_INSTRUCTIONS_LINK")`` dynamically.
        """
        link = getattr(self._settings, "permission_instructions_link", "") or ""
        return link or None

    def get_table_access_error_object(
        self,
        tables: set[Any],
    ) -> SupersetError:
        """Return the SupersetError for the denied SQL tables."""
        return SupersetError(
            error_type=SupersetErrorType.TABLE_SECURITY_ACCESS_ERROR,
            message=self.get_table_access_error_msg(tables),
            level=ErrorLevel.WARNING,
            extra={
                "link": self.get_table_access_link(tables),
                "tables": [str(table) for table in tables],
            },
        )

    # --- Ownership checks ---

    async def raise_for_ownership(
        self,
        resource: Any,
        user_id: int | None,
    ) -> None:
        """Raise SupersetSecurityException if user is not owner and not admin.

        Admin users bypass the ownership check entirely, mirroring
        Superset's ``raise_for_ownership()`` behaviour.
        """
        # 1:1 with the original ``raise_for_ownership``: a missing-ownership
        # denial carries a ``SupersetError(MISSING_OWNERSHIP_ERROR)`` payload.
        # ``SupersetSecurityException`` takes that error object positionally —
        # the previous ``message=`` kwarg raised ``TypeError`` (→ HTTP 500)
        # whenever the check actually denied; it was masked only because the
        # ``is_owner`` lazy-load of ``owners`` crashed first.
        if user_id is None:
            raise SupersetSecurityException(
                SupersetError(
                    error_type=SupersetErrorType.MISSING_OWNERSHIP_ERROR,
                    message="Authentication required to modify this resource.",
                    level=ErrorLevel.ERROR,
                )
            )
        # Fetch user to check admin role
        user = await self.find_user_by_id(user_id)
        if user is not None and self.is_admin(user):
            return
        # Pre-load ``owners`` so the sync ``is_owner`` read below doesn't trip a
        # MissingGreenlet when the command fetched the resource without
        # eager-loading owners (e.g. bare ``dao.find_by_id`` in the delete /
        # refresh / column-metric commands).
        await self._ensure_relationship_loaded(resource, "owners")
        if self.is_owner(resource, user_id):
            return
        # Friendly resource label (the original relied on the datasource
        # ``__repr__`` → name; the new models have none, so derive from the
        # standard name columns rather than leaking a raw object repr).
        resource_label = (
            getattr(resource, "table_name", None)
            or getattr(resource, "slice_name", None)
            or getattr(resource, "dashboard_title", None)
            or getattr(resource, "database_name", None)
            or type(resource).__name__
        )
        raise SupersetSecurityException(
            SupersetError(
                error_type=SupersetErrorType.MISSING_OWNERSHIP_ERROR,
                message=f"You don't have the rights to alter {resource_label}",
                level=ErrorLevel.ERROR,
            )
        )

    # --- Guest user checks ---

    def is_guest_user(self, user: Any | None = None) -> bool:
        """Check if the given user is a guest user (JWT-authenticated).

        Mirrors the original ``SupersetSecurityManager.is_guest_user``:
        returns False unless the EMBEDDED_SUPERSET feature flag is enabled.
        """
        if not self._embedded_superset_enabled:
            return False
        if user is None:
            return False
        return getattr(user, "is_guest", False)

    async def has_guest_access(self, dashboard: Any, *, user: Any) -> bool:
        """Check if a guest user has access to a specific dashboard."""
        if not self.is_guest_user(user):
            return False
        resources = getattr(user, "resources", [])
        # Check integer ID first (matches Superset priority)
        dashboard_id = getattr(dashboard, "id", None)
        if dashboard_id is not None:
            for r in resources:
                if r.get("type") == "dashboard" and str(r.get("id")) == str(
                    dashboard_id
                ):
                    return True
        # Then check UUID from embedded config
        embedded = getattr(dashboard, "embedded", None)
        if embedded:
            embedded_uuid = str(embedded[0].uuid)
            for r in resources:
                if r.get("type") == "dashboard" and str(r.get("id")) == embedded_uuid:
                    return True
        return False

    async def can_drill_dataset_via_dashboard_access(
        self, dataset: Any, dashboard: Any, *, user: Any
    ) -> bool:
        """Return True if an embedded/DASHBOARD_RBAC user can drill a dataset.

        Mirrors the original ``SupersetSecurityManager
        .can_drill_dataset_via_dashboard_access`` exactly: a guest user (with
        EMBEDDED_SUPERSET enabled) who has guest access to the dashboard, *or* a
        DASHBOARD_RBAC user whose roles intersect a published dashboard's roles,
        may drill — but only if ``dataset`` is one of the dashboard's
        datasources. Fails closed (returns False) otherwise.
        """
        # First branch: embedded guest access.
        access_via_dashboard = (
            self._embedded_superset_enabled
            and self.is_guest_user(user)
            and await self.has_guest_access(dashboard, user=user)
        )

        # Second branch: DASHBOARD_RBAC role intersection.
        if not access_via_dashboard:
            dashboard_roles = getattr(dashboard, "roles", [])
            if (
                self._dashboard_rbac_enabled
                and dashboard_roles
                and getattr(dashboard, "published", False)
            ):
                user_role_ids = {r.id for r in await self.get_user_roles(user)}
                dashboard_role_ids = {role.id for role in dashboard_roles}
                access_via_dashboard = bool(user_role_ids & dashboard_role_ids)

        if not access_via_dashboard:
            return False

        # The dataset must belong to the dashboard. The original iterates
        # ``dashboard.datasources`` (a property aggregating slice datasources);
        # here we enumerate the dashboard's slice ``datasource_id`` values
        # through the async session and compare by id, matching the original
        # ``dataset.id in {dataset.id for dataset in dashboard.datasources}``.
        await self._ensure_relationship_loaded(dashboard, "slices")
        dashboard_dataset_ids = {
            slc.datasource_id
            for slc in (getattr(dashboard, "slices", None) or [])
            if slc.datasource_id is not None
        }
        return getattr(dataset, "id", None) in dashboard_dataset_ids

    # --- Anonymous/Public user ---

    def get_anonymous_user(self) -> Any:
        """Return an AnonymousUser with the PUBLIC role."""
        from superset.middleware.auth import UnauthenticatedUser

        return UnauthenticatedUser(is_authenticated=False)

    # --- Catalog access ---

    async def can_access_catalog(
        self, database: Any, catalog: str, *, user: Any
    ) -> bool:
        """Check if user can access a specific catalog within a database."""
        if await self.can_access_database(database, user=user):
            return True
        db_name = getattr(database, "database_name", "")
        catalog_perm = f"[{db_name}].[{catalog}]"
        return await self.has_access(CATALOG_ACCESS, catalog_perm, user=user)

    # --- Chart access ---

    async def can_access_chart(self, chart: Any, *, user: Any) -> bool:
        """Check if user can access a chart."""
        if self.is_admin(user):
            return True
        if self.is_owner(chart, user):
            return True
        datasource = getattr(chart, "datasource", None)
        if datasource:
            return await self.can_access_datasource(datasource, user=user)
        return False

    # --- Guest token management ---

    @staticmethod
    def create_guest_access_token(
        *,
        secret_key: str,
        user: dict[str, Any],
        resources: list[dict[str, Any]],
        rls: list[dict[str, Any]],
        algorithm: str = "HS256",
        exp_seconds: int = 300,
        audience: str = "",
    ) -> str:
        """Create a guest access JWT token.

        Delegates to superset.security.guest.create_guest_access_token.
        Controllers call this via security_manager.create_guest_access_token().
        """
        from superset.security.guest import create_guest_access_token

        return create_guest_access_token(
            secret_key=secret_key,
            user=user,
            resources=resources,
            rls=rls,
            algorithm=algorithm,
            exp_seconds=exp_seconds,
            audience=audience,
        )

    @staticmethod
    def parse_jwt_guest_token(
        token: str,
        secret_key: str,
        algorithm: str = "HS256",
    ) -> dict[str, Any] | None:
        """Parse and validate a guest JWT token.

        Delegates to superset.security.guest.parse_guest_token.
        """
        from superset.security.guest import parse_guest_token

        return parse_guest_token(token, secret_key, algorithm=algorithm)

    def get_guest_user_from_request(self, request: Any) -> Any | None:
        """Extract GuestUser from a request if JWT-authenticated.

        Returns the GuestUser from request.user if is_guest is True,
        otherwise None.
        """
        user = getattr(request, "user", None)
        if user is not None and self.is_guest_user(user):
            return user
        return None


# ---------------------------------------------------------------------------
# Sync proxy
# ---------------------------------------------------------------------------
#
# ``SQL_QUERY_MUTATOR`` is a user-supplied callable (configured in
# ``superset_config.py``) that the original Superset invokes inside the
# *synchronous* ``Database.mutate_sql_based_on_config`` code path.  It
# is given ``security_manager`` as a kwarg so the mutator can read the
# current user, check role/permission membership, etc.
#
# Liteset's :class:`AsyncSecurityManager` is request-scoped (DI'd from
# Litestar) and async — so we cannot pass it directly to a sync
# callback.  Instead we expose a small synchronous read-only proxy:
# the methods most mutators care about (``get_user_id``,
# ``is_user_admin``, ``current_user``) all have synchronous answers
# already, since they read from :mod:`superset.utils.core`'s
# user-context :class:`ContextVar` which is populated by
# :mod:`superset.middleware.auth` before any DB call runs.  More
# elaborate methods (``has_access``, etc.) are intentionally
# unavailable from the sync proxy — mutators that need them should
# move to the async pipeline.


class SyncSecurityManagerProxy:
    """Sync read-only adapter for :class:`AsyncSecurityManager`.

    Designed for ``SQL_QUERY_MUTATOR`` callbacks invoked from the
    synchronous ``Database.mutate_sql_based_on_config`` path. Mirrors
    the read-only API surface most mutators actually need:

    - ``get_user_id()`` → current user's primary key, or ``None`` when
      unauthenticated (e.g. Celery task / Alembic migration).
    - ``current_user`` → the live user object held on the request
      :class:`ContextVar`.
    - ``is_user_admin()`` → whether the current user has the Admin
      role (delegates to :meth:`AsyncSecurityManager.is_admin`).

    Aliases ``current_user_id`` / ``is_admin`` are provided for
    parity with the original
    :class:`SupersetSecurityManager` attribute names.
    """

    def __init__(self, async_sm: AsyncSecurityManager | None = None) -> None:
        self._async = async_sm

    # ── User-context lookups ────────────────────────────────────────
    @staticmethod
    def get_user_id() -> int | None:
        """Return the current user's primary key, or ``None``."""
        from superset.utils.core import get_user_id

        return get_user_id()

    # 1:1 alias with original ``SupersetSecurityManager.current_user_id``.
    @property
    def current_user_id(self) -> int | None:
        return self.get_user_id()

    @property
    def current_user(self) -> Any | None:
        """Return the live user object on the request ContextVar."""
        from superset.utils.core import get_current_user

        return get_current_user()

    # ── Role membership ─────────────────────────────────────────────
    def is_user_admin(self) -> bool:
        """Return ``True`` if the current user is an Admin."""
        user = self.current_user
        if user is None:
            return False
        if self._async is not None:
            return self._async.is_admin(user)
        # Fall back to inspecting role names directly when no async
        # SM is attached (e.g. during early bootstrap / Celery).
        return any(
            getattr(r, "name", None) == "Admin"
            for r in getattr(user, "roles", []) or []
        )

    # 1:1 alias with original ``SupersetSecurityManager.is_admin``.
    def is_admin(self) -> bool:
        return self.is_user_admin()


def get_sync_security_manager_proxy() -> SyncSecurityManagerProxy:
    """Return a fresh :class:`SyncSecurityManagerProxy`.

    Intentionally constructs without a bound :class:`AsyncSecurityManager`
    instance — the proxy's read-only methods rely on the user
    :class:`ContextVar` from :mod:`superset.utils.core` and do not
    require an async session.  Call sites that *do* have an
    async-SM in hand can construct ``SyncSecurityManagerProxy(async_sm)``
    directly to enable :meth:`is_user_admin` to use the configured
    admin role name from the async manager.
    """
    return SyncSecurityManagerProxy()
