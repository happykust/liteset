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
"""Shared fixtures for integration tests of core API controllers.

Creates minimal Litestar apps with fully mocked dependencies so tests
can exercise the HTTP pipeline (request -> controller -> response) without
a real database connection.

Strategy: Temporarily replace each controller's ``dependencies`` dict with
one that provides mock DAOs. This avoids triggering the real DAO constructors
(and their Flask import chain) while preserving the controller's route
registration.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from litestar import Litestar

# ---------------------------------------------------------------------------
# Workaround: tell Litestar to skip msgspec validation for DI parameters.
#
# Litestar's ``SignatureModel`` validates ALL handler kwargs through
# ``msgspec.convert()`` — including DI-injected ones.  When mock objects
# (``MagicMock``, ``MockDAO``, ``MockUser``) are injected for parameters
# typed as ``Protocol`` subclasses (``SecurityManagerProtocol``,
# ``UserProtocol``, etc.), msgspec cannot validate them because
# ``isinstance(mock, SomeProtocol)`` raises ``TypeError`` for
# non-runtime-checkable protocols.
#
# Litestar already skips validation for parameter *names* listed in the
# internal ``SKIP_VALIDATION_NAMES`` set (``request``, ``scope``, etc.)
# by normalising their annotation to ``Any``.  We temporarily add our
# DI dependency names to that set while building the test ``Litestar``
# app so that the generated signature models use ``Any`` for those
# fields.  After the app is created the signature models are frozen, so
# we can safely restore the original set.
# ---------------------------------------------------------------------------
# Reference to the internal set that ``_normalize_annotation`` consults.
from litestar._signature.model import (
    _normalize_annotation as _norm_fn,  # noqa: F401 – only used to reach __globals__
)
from litestar.di import Provide
from litestar.middleware import ASGIMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

from superset.exceptions import (
    generic_exception_handler,
    superset_exception_handler,
    SupersetException,
)

_SKIP_VALIDATION_NAMES: set[str] = _norm_fn.__globals__["SKIP_VALIDATION_NAMES"]

# Dependency parameter names used across all controllers.
_DI_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "dao",
        "ds_dao",
        "embedded_dao",
        "kv_dao",
        "column_dao",
        "metric_dao",
        "security_manager",
        "current_user",
        "session",
        "request_cache",
        "rison_params",
    }
)


@contextmanager
def _skip_di_validation():
    """Temporarily add DI param names to Litestar's skip-validation set.

    While the context manager is active, ``_normalize_annotation`` will
    return ``Any`` for every handler parameter whose name is in
    ``_DI_PARAM_NAMES``, preventing msgspec from trying to validate mock
    objects injected by the test harness.

    This is safe because:
    * ``Litestar.__init__`` builds and freezes signature models
      synchronously, so the names only need to be present during the
      constructor call.
    * The conftest already warns that ``create_test_app`` is NOT
      thread-safe, so concurrent mutation is not a concern.
    """
    skip_set = _SKIP_VALIDATION_NAMES
    skip_set.update(_DI_PARAM_NAMES)
    try:
        yield
    finally:
        skip_set.difference_update(_DI_PARAM_NAMES)


@dataclass
class MockUser:
    """Authenticated mock user with all core-API permissions."""

    id: int = 1
    username: str = "admin"
    email: str = "admin@test.com"
    is_authenticated: bool = True
    permissions: set[tuple[str, str]] = field(
        default_factory=lambda: {
            ("can_read", "Chart"),
            ("can_write", "Chart"),
            ("can_read", "Dashboard"),
            ("can_write", "Dashboard"),
            ("can_read", "Database"),
            ("can_write", "Database"),
            ("can_read", "Dataset"),
            ("can_write", "Dataset"),
            ("can_read", "Query"),
            ("can_write", "Query"),
            ("can_read", "SavedQuery"),
            ("can_write", "SavedQuery"),
            ("can_read", "SQLLab"),
            ("can_write", "SQLLab"),
            ("can_sqllab", "Superset"),
            ("can_read", "DashboardFilterStateRestApi"),
            ("can_write", "DashboardFilterStateRestApi"),
            ("can_read", "DashboardPermalinkRestApi"),
            ("can_write", "DashboardPermalinkRestApi"),
            ("can_read", "SqlLabPermalinkRestApi"),
            ("can_write", "SqlLabPermalinkRestApi"),
        }
    )


@dataclass
class MockLimitedUser:
    """User with read-only Chart permission -- no write, no other resources."""

    id: int = 2
    username: str = "viewer"
    email: str = "viewer@test.com"
    is_authenticated: bool = True
    permissions: set[tuple[str, str]] = field(
        default_factory=lambda: {("can_read", "Chart")}
    )


class InjectMockUserMiddleware(ASGIMiddleware):
    """Middleware that injects a MockUser into the ASGI scope.

    Replaces the real ``SupersetAuthMiddleware`` so RBAC guards work
    without cookie/JWT decoding.
    """

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        if scope["type"] in ("http", "websocket"):
            scope["user"] = MockUser()
            scope["auth"] = "mock"
        await next_app(scope, receive, send)


class MockDAO:
    """Mock DAO that satisfies all Protocol types used by controllers.

    Uses a real class (not AsyncMock) so that Litestar's msgspec-based
    signature validation can handle it. All async methods are AsyncMock
    instances assigned as attributes.
    """

    def __init__(self) -> None:
        self.model_cls = None
        self._session = AsyncMock()
        self._session.add = MagicMock()
        self._session.flush = AsyncMock()
        self._session.delete = AsyncMock()
        self._session.scalar = AsyncMock(return_value=0)
        self._session.execute = AsyncMock()
        # CRUD methods
        self.find_all = AsyncMock(return_value=[])
        self.find_by_id = AsyncMock(return_value=None)
        self.find_by_ids = AsyncMock(return_value=[])
        self.count = AsyncMock(return_value=0)
        self.find_one_or_none = AsyncMock(return_value=None)
        self.update = AsyncMock(return_value=MagicMock())
        self.delete = AsyncMock()
        self.bulk_delete = AsyncMock(return_value=0)
        # Extended methods
        self.get_by_id_or_uuid = AsyncMock(return_value=None)
        self.get_by_id_or_slug = AsyncMock(return_value=None)
        self.favorited_ids = AsyncMock(return_value=[])
        self.is_favorited_by = AsyncMock(return_value=False)
        self.add_favorite = AsyncMock()
        self.remove_favorite = AsyncMock()
        self.get_datasets_for_dashboard = AsyncMock(return_value=[])
        self.get_charts_for_dashboard = AsyncMock(return_value=[])
        self.get_related_objects = AsyncMock(
            return_value={
                "charts": [],
                "dashboards": [],
                "sqllab_tab_states": [],
            }
        )
        self.get_ssh_tunnel = AsyncMock(return_value=None)
        self.get_queries_changed_after = AsyncMock(return_value=[])
        self.has_permission_view = AsyncMock(return_value=False)
        # DashboardDAOProtocol extended methods
        self.copy_dashboard = AsyncMock(return_value=MagicMock())
        self.update_colors_config = AsyncMock(return_value=MagicMock())
        # DashboardDAOProtocol slug validation
        self.validate_slug_uniqueness = AsyncMock(return_value=True)
        self.validate_update_slug_uniqueness = AsyncMock(return_value=True)
        # DatabaseDAOProtocol extended methods
        self.validate_uniqueness = AsyncMock(return_value=True)
        self.validate_update_uniqueness = AsyncMock(return_value=True)
        self.create = AsyncMock(
            return_value=MagicMock(
                id=1,
                slice_name="mock",
                viz_type="table",
                table_name="mock",
                dashboard_title="mock",
                slug=None,
                label="mock",
                sql="SELECT 1",
                params=None,
                cache_timeout=None,
                query_context=None,
                description=None,
                published=False,
                position_json=None,
                css=None,
                json_metadata=None,
            )
        )
        # QueryDAOProtocol
        self.stop_query = AsyncMock(return_value=None)
        # KeyValueDAOProtocol
        self.set_value = AsyncMock()
        self.get_value = AsyncMock(return_value=None)
        self.delete_value = AsyncMock(return_value=False)
        # ColumnDAOProtocol / MetricDAOProtocol
        self.find_by_dataset_and_id = AsyncMock(return_value=None)
        # EmbeddedDashboardDAO
        self.find_by_dashboard_id = AsyncMock(return_value=None)
        self.upsert = AsyncMock(return_value=None)
        # DatasetDAOProtocol extended
        self.get_database_by_id = AsyncMock(return_value=None)
        # DatasourceDAOProtocol
        self.get_datasource = AsyncMock(return_value=None)

    @property
    def session(self) -> Any:
        return self._session


def make_mock_dao() -> MockDAO:
    """Create a fully-stubbed DAO mock that satisfies CRUDDAOProtocol."""
    return MockDAO()


# All known dependency names used across controllers
_MOCK_DEP_NAMES = [
    "dao",
    "ds_dao",
    "embedded_dao",
    "kv_dao",
    "column_dao",
    "metric_dao",
    "rison_params",
    "security_manager",
]


def _make_mock_deps(mock_dao: MockDAO) -> dict[str, Provide]:
    """Build the standard set of mock DI dependencies for test apps."""
    mock_security_manager = MagicMock()
    mock_security_manager.raise_for_access = AsyncMock()
    mock_security_manager.is_admin = MagicMock(return_value=True)
    mock_security_manager.find_user_by_id = AsyncMock(return_value=MagicMock(id=1))
    mock_security_manager.has_access = AsyncMock(return_value=True)
    mock_security_manager.can_access_dashboard = AsyncMock(return_value=True)
    mock_security_manager.get_accessible_datasource_ids = AsyncMock(return_value=None)
    mock_security_manager.get_accessible_database_ids = AsyncMock(return_value=None)
    mock_security_manager.can_access = AsyncMock(return_value=True)
    mock_security_manager.can_access_all_queries = AsyncMock(return_value=True)

    return {
        "dao": Provide(lambda: mock_dao, sync_to_thread=False),
        "ds_dao": Provide(lambda: make_mock_dao(), sync_to_thread=False),
        "embedded_dao": Provide(lambda: make_mock_dao(), sync_to_thread=False),
        "kv_dao": Provide(lambda: make_mock_dao(), sync_to_thread=False),
        "column_dao": Provide(lambda: make_mock_dao(), sync_to_thread=False),
        "metric_dao": Provide(lambda: make_mock_dao(), sync_to_thread=False),
        "rison_params": Provide(lambda: None, sync_to_thread=False),
        "security_manager": Provide(
            lambda: mock_security_manager, sync_to_thread=False
        ),
    }


def create_test_app(*controllers: Any) -> Litestar:
    """Create a minimal Litestar app with mocked dependencies for testing.

    Temporarily replaces controller-level ``dependencies`` with mock
    providers, then builds the Litestar app. The controller class
    ``dependencies`` are restored after the app is created (Litestar
    snapshots them during ``__init__``).

    WARNING: This function temporarily mutates class-level ``dependencies``
    dicts on the controller classes.  It is NOT thread-safe and is
    incompatible with ``pytest-xdist`` parallel execution.  If parallel
    test runs are ever needed, refactor to use controller subclasses or
    Litestar's ``app.dependencies`` override instead of class mutation.
    """
    mock_user = MockUser()
    mock_dao = make_mock_dao()
    mock_deps = _make_mock_deps(mock_dao)

    # Save and replace controller dependencies
    originals: list[tuple[type, dict[str, Any]]] = []
    handler_originals: list[tuple[Any, dict[str, Any] | None]] = []
    for ctrl in controllers:
        originals.append((ctrl, getattr(ctrl, "dependencies", {})))
        ctrl_keys = set(getattr(ctrl, "dependencies", {}).keys())
        ctrl.dependencies = {k: v for k, v in mock_deps.items() if k in ctrl_keys}
        # Also patch endpoint-level deps (e.g. rison_params on specific handlers)
        for attr_name in dir(ctrl):
            attr = getattr(ctrl, attr_name, None)
            if hasattr(attr, "fn") and hasattr(attr.fn, "dependencies"):
                handler_originals.append(
                    (attr.fn, getattr(attr.fn, "dependencies", None))
                )
                if attr.fn.dependencies:
                    patched = {
                        k: mock_deps.get(k, v) for k, v in attr.fn.dependencies.items()
                    }
                    attr.fn.dependencies = patched

    mock_security_manager = MagicMock()
    mock_security_manager.raise_for_access = AsyncMock()
    mock_security_manager.get_rls_cache_key = AsyncMock(return_value="")
    mock_security_manager.is_admin = MagicMock(return_value=True)
    mock_security_manager.can_access_all_queries = AsyncMock(return_value=True)
    mock_security_manager.get_schemas_accessible_by_user = AsyncMock(return_value=[])
    mock_security_manager.is_owner = MagicMock(return_value=True)
    mock_security_manager.raise_for_ownership = AsyncMock(return_value=None)
    mock_security_manager.find_user_by_id = AsyncMock(return_value=MagicMock(id=1))
    mock_security_manager.has_access = AsyncMock(return_value=True)
    mock_security_manager.can_access_dashboard = AsyncMock(return_value=True)
    mock_security_manager.can_access = AsyncMock(return_value=True)
    mock_security_manager.get_accessible_datasource_ids = AsyncMock(return_value=None)
    mock_security_manager.get_accessible_database_ids = AsyncMock(return_value=None)

    try:
        with _skip_di_validation():
            app = Litestar(
                route_handlers=list(controllers),
                dependencies={
                    "session": Provide(lambda: MagicMock(), sync_to_thread=False),
                    "current_user": Provide(lambda: mock_user, sync_to_thread=False),
                    "request_cache": Provide(lambda: {}, sync_to_thread=False),
                    "rison_params": Provide(lambda: None, sync_to_thread=False),
                    "security_manager": Provide(
                        lambda: mock_security_manager, sync_to_thread=False
                    ),
                },
                middleware=[InjectMockUserMiddleware()],
                exception_handlers={
                    SupersetException: superset_exception_handler,
                    Exception: generic_exception_handler,
                },
            )
    finally:
        # Restore originals so the controller classes aren't permanently modified
        for ctrl, orig in originals:
            ctrl.dependencies = orig
        for handler, orig_deps in handler_originals:
            handler.dependencies = orig_deps

    return app


def create_test_app_limited(*controllers: Any) -> Litestar:
    """Create a minimal Litestar app with a **limited-permission** user.

    The injected ``MockLimitedUser`` has only ``can_read_Chart``, so
    write operations and non-Chart resources should return 403.
    """
    limited_user = MockLimitedUser()
    mock_dao = make_mock_dao()
    mock_deps = _make_mock_deps(mock_dao)

    originals: list[tuple[type, dict[str, Any]]] = []
    handler_originals: list[tuple[Any, dict[str, Any] | None]] = []
    for ctrl in controllers:
        originals.append((ctrl, getattr(ctrl, "dependencies", {})))
        ctrl_keys = set(getattr(ctrl, "dependencies", {}).keys())
        ctrl.dependencies = {k: v for k, v in mock_deps.items() if k in ctrl_keys}
        for attr_name in dir(ctrl):
            attr = getattr(ctrl, attr_name, None)
            if hasattr(attr, "fn") and hasattr(attr.fn, "dependencies"):
                handler_originals.append(
                    (attr.fn, getattr(attr.fn, "dependencies", None))
                )
                if attr.fn.dependencies:
                    patched = {
                        k: mock_deps.get(k, v) for k, v in attr.fn.dependencies.items()
                    }
                    attr.fn.dependencies = patched

    mock_sm = MagicMock()
    mock_sm.raise_for_access = AsyncMock()
    mock_sm.get_rls_cache_key = AsyncMock(return_value="")
    mock_sm.is_admin = MagicMock(return_value=False)

    try:
        with _skip_di_validation():
            app = Litestar(
                route_handlers=list(controllers),
                dependencies={
                    "session": Provide(lambda: MagicMock(), sync_to_thread=False),
                    "current_user": Provide(lambda: limited_user, sync_to_thread=False),
                    "request_cache": Provide(lambda: {}, sync_to_thread=False),
                    "rison_params": Provide(lambda: None, sync_to_thread=False),
                    "security_manager": Provide(lambda: mock_sm, sync_to_thread=False),
                },
                middleware=[InjectLimitedUserMiddleware()],
                exception_handlers={
                    SupersetException: superset_exception_handler,
                    Exception: generic_exception_handler,
                },
            )
    finally:
        for ctrl, orig in originals:
            ctrl.dependencies = orig
        for handler, orig_deps in handler_originals:
            handler.dependencies = orig_deps

    return app


class InjectLimitedUserMiddleware(ASGIMiddleware):
    """Injects a user with limited permissions for negative-path testing."""

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        if scope["type"] in ("http", "websocket"):
            scope["user"] = MockLimitedUser()
            scope["auth"] = "mock"
        await next_app(scope, receive, send)


class InjectUnauthenticatedUserMiddleware(ASGIMiddleware):
    """Middleware that injects an unauthenticated user into ASGI scope.

    Used by ``create_test_app_no_auth`` to simulate requests that arrive
    without a valid session cookie or JWT token.  The RBAC guards check
    ``user.is_authenticated`` and raise ``PermissionDeniedException`` (403)
    for such users; combining this with an explicit
    ``NotAuthorizedException`` re-raise in the middleware gives true 401
    semantics that match the production ``SupersetAuthMiddleware`` behaviour.
    """

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        if scope["type"] in ("http", "websocket"):
            from litestar.exceptions import NotAuthorizedException

            # Raise here (before the handler runs) so Litestar turns it
            # into an HTTP 401 response — the same path taken by the real
            # SupersetAuthMiddleware when no credentials are present.
            raise NotAuthorizedException(detail="Not authenticated")
        await next_app(scope, receive, send)


def create_test_app_no_auth(*controllers: Any) -> Litestar:
    """Create a minimal Litestar app **without** mock-user injection.

    Requests to this app will receive a 401 response from the ASGI
    middleware before any controller handler or RBAC guard is evaluated,
    matching the production auth flow for unauthenticated clients.
    """
    mock_dao = make_mock_dao()
    mock_deps = _make_mock_deps(mock_dao)

    originals: list[tuple[type, dict[str, Any]]] = []
    handler_originals: list[tuple[Any, dict[str, Any] | None]] = []
    for ctrl in controllers:
        originals.append((ctrl, getattr(ctrl, "dependencies", {})))
        ctrl_keys = set(getattr(ctrl, "dependencies", {}).keys())
        ctrl.dependencies = {k: v for k, v in mock_deps.items() if k in ctrl_keys}
        for attr_name in dir(ctrl):
            attr = getattr(ctrl, attr_name, None)
            if hasattr(attr, "fn") and hasattr(attr.fn, "dependencies"):
                handler_originals.append(
                    (attr.fn, getattr(attr.fn, "dependencies", None))
                )
                if attr.fn.dependencies:
                    patched = {
                        k: mock_deps.get(k, v) for k, v in attr.fn.dependencies.items()
                    }
                    attr.fn.dependencies = patched

    try:
        with _skip_di_validation():
            app = Litestar(
                route_handlers=list(controllers),
                dependencies={
                    "session": Provide(lambda: MagicMock(), sync_to_thread=False),
                    "current_user": Provide(lambda: MagicMock(), sync_to_thread=False),
                    "request_cache": Provide(lambda: {}, sync_to_thread=False),
                    "rison_params": Provide(lambda: None, sync_to_thread=False),
                },
                middleware=[InjectUnauthenticatedUserMiddleware()],
                exception_handlers={
                    SupersetException: superset_exception_handler,
                    Exception: generic_exception_handler,
                },
            )
    finally:
        for ctrl, orig in originals:
            ctrl.dependencies = orig
        for handler, orig_deps in handler_originals:
            handler.dependencies = orig_deps

    return app


@pytest.fixture
def mock_dao() -> AsyncMock:
    return make_mock_dao()


@pytest.fixture
def mock_user() -> MockUser:
    return MockUser()
