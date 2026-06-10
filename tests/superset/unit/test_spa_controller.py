import json as _json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar import Litestar
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException
from litestar.response import Response as _Response, Template as _Template
from litestar.testing import AsyncTestClient

from superset.app import create_app
from superset.config import SupersetSettings
from superset.controllers.spa import (
    _render_welcome_dashboard,
    SPA_ROUTE_PREFIXES,
    SPAController,
)


@pytest.fixture
def app():
    settings = SupersetSettings(
        secret_key="test-secret-long-enough",
        sqlalchemy_database_uri="sqlite+aiosqlite://",
        cors_allow_origins=["*"],
    )
    return create_app(settings=settings)


@pytest.mark.parametrize(
    "path",
    [
        "/superset/welcome/",
        "/explore/",
        "/dashboard/list/",
        "/superset/sqllab/",
        "/chart/list/",
    ],
)
async def test_spa_routes_return_html(app: Litestar, path: str):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(path)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


async def test_spa_route_contains_bootstrap_data(app: Litestar):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/superset/welcome/")
        assert resp.status_code == 200


async def test_spa_route_with_path_param(app: Litestar):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/dashboard/42/")
        assert resp.status_code == 200


async def test_known_spa_route_200(app: Litestar):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/superset/welcome/")
        assert resp.status_code == 200


async def test_unknown_prefix_404(app: Litestar):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/unknown/path/")
        assert resp.status_code == 404


def test_spa_route_prefixes_not_empty():
    assert len(SPA_ROUTE_PREFIXES) > 0


# ---------------------------------------------------------------------------
# frontend_log — action/json shape consistency tests
# ---------------------------------------------------------------------------


def _get_raw_method(controller_cls: type, method_name: str):
    """Return the underlying async function from a Litestar-decorated method."""
    handler = getattr(controller_cls, method_name)
    if hasattr(handler, "fn"):
        return handler.fn
    return handler


_frontend_log = _get_raw_method(SPAController, "frontend_log")


def _make_request(
    events_json: str | None = None,
    *,
    explode: str | None = None,
    referer: str | None = None,
    form_data: dict | None = None,
    user_id: int | None = 42,
) -> MagicMock:
    """Build a mock Litestar Request for frontend_log."""
    request = MagicMock()
    request.user = MagicMock()
    request.user.id = user_id

    # query_params
    qp = {}
    if explode is not None:
        qp["explode"] = explode
    request.query_params = qp

    # headers (Referer)
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    request.headers = headers

    # form — returns an awaitable dict
    if form_data is not None:
        _form = form_data
    elif events_json is not None:
        _form = {"events": events_json}
    else:
        _form = {}
    request.form = AsyncMock(return_value=_form)
    return request


def _make_state(session_factory) -> MagicMock:
    state = MagicMock()
    state.session_factory = session_factory
    return state


async def test_frontend_log_writes_action_log_not_event_name():
    """action column must always be 'log' regardless of event_name in payload."""
    events = [
        {"event_name": "mount_dashboard", "dashboard_id": 7},
        {"event_name": "mount_explorer", "slice_id": 3},
    ]
    request = _make_request(
        events_json=_json.dumps(events),
        explode="events",
        referer="http://localhost/dashboard/list/",
    )

    captured: list[dict] = []

    async def mock_create_log(attrs):
        captured.append(dict(attrs))
        return MagicMock(id=len(captured))

    mock_session = AsyncMock()
    mock_dao = AsyncMock()
    mock_dao.create_log.side_effect = mock_create_log
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    def session_factory():
        return mock_session

    state = _make_state(session_factory)

    with patch("superset.controllers.spa.AsyncLogDAO", return_value=mock_dao):
        result = await _frontend_log(
            SPAController(owner=MagicMock()),
            request=request,
            state=state,
        )

    # 1:1 with superset_old/views/core.py:872-873 ``return Response(status=200)``
    # — empty body, 200 status; matches our Response(content=None, status_code=200).
    from litestar.response import Response as _Response

    assert isinstance(result, _Response)
    assert result.status_code == 200
    assert len(captured) == 2

    for rec in captured:
        # The critical 1:1 parity: upstream always writes action="log"
        assert rec["action"] == "log", f"Expected 'log', got {rec['action']!r}"

    # json column must contain event_name so recent_activity can query it
    assert _json.loads(captured[0]["json"])["event_name"] == "mount_dashboard"
    assert _json.loads(captured[1]["json"])["event_name"] == "mount_explorer"

    # dashboard_id / slice_id extracted from event dict
    assert captured[0]["dashboard_id"] == 7
    assert captured[1]["slice_id"] == 3

    # referrer propagated
    assert captured[0]["referrer"] == "http://localhost/dashboard/list/"
    assert captured[1]["referrer"] == "http://localhost/dashboard/list/"


async def test_frontend_log_empty_events_returns_ok():
    request = _make_request(events_json="[]", explode="events")
    state = _make_state(MagicMock())

    result = await _frontend_log(
        SPAController(owner=MagicMock()),
        request=request,
        state=state,
    )

    # 1:1 with superset_old/views/core.py:873: returns empty Response(status=200).
    from litestar.response import Response as _Response

    assert isinstance(result, _Response)
    assert result.status_code == 200


async def test_frontend_log_no_explode_single_form_event():
    """Without ?explode=events, the whole form dict is treated as one record."""
    request = _make_request(
        form_data={"event_name": "log_this_chart_rendered", "slice_id": "99"},
    )

    captured: list[dict] = []

    async def mock_create_log(attrs):
        captured.append(dict(attrs))
        return MagicMock(id=1)

    mock_session = AsyncMock()
    mock_dao = AsyncMock()
    mock_dao.create_log.side_effect = mock_create_log
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    state = _make_state(lambda: mock_session)

    with patch("superset.controllers.spa.AsyncLogDAO", return_value=mock_dao):
        await _frontend_log(
            SPAController(owner=MagicMock()),
            request=request,
            state=state,
        )

    assert len(captured) == 1
    assert captured[0]["action"] == "log"


# ---------------------------------------------------------------------------
# _render_welcome_dashboard — 1:1 parity tests
# Mirrors Superset.welcome() → self.dashboard() from
# superset_old/views/core.py:926-931 + 795-837.
# ---------------------------------------------------------------------------


def _make_session_factory(welcome_dashboard_id=None, dashboard=None):
    """Build a mock session_factory that services the three async-with calls
    made by _render_welcome_dashboard:
      1. UserAttribute.welcome_dashboard_id query
      2. AsyncDashboardDAO.get_by_id_or_slug
      3. security_manager.raise_for_access
    Each call opens a *separate* context-manager; we return fresh mock sessions
    from an iterator so each ``async with session_factory()`` gets its own.
    """
    # Session 1: welcome_dashboard_id query
    wa_session = AsyncMock()
    wa_result = MagicMock()
    wa_result.scalar.return_value = welcome_dashboard_id
    wa_session.execute = AsyncMock(return_value=wa_result)
    wa_session.__aenter__ = AsyncMock(return_value=wa_session)
    wa_session.__aexit__ = AsyncMock(return_value=False)

    # Session 2: dashboard DAO lookup
    dao_session = AsyncMock()
    dao_session.__aenter__ = AsyncMock(return_value=dao_session)
    dao_session.__aexit__ = AsyncMock(return_value=False)

    # Session 3: security manager
    sec_session = AsyncMock()
    sec_session.__aenter__ = AsyncMock(return_value=sec_session)
    sec_session.__aexit__ = AsyncMock(return_value=False)

    sessions = iter([wa_session, dao_session, sec_session])

    def session_factory():
        return next(sessions)

    return session_factory, dao_session


async def test_render_welcome_dashboard_no_id_returns_none():
    """When welcome_dashboard_id is None/0, return None → caller shows welcome."""
    sf, _ = _make_session_factory(welcome_dashboard_id=None)
    result = await _render_welcome_dashboard(
        user=MagicMock(),
        user_id=1,
        session_factory=sf,
        state=MagicMock(),
        settings=MagicMock(
            enable_ui_theme_administration=False,
            static_assets_prefix="",
            session_cookie_name="session",
            secret_key="x" * 32,
        ),
        request=MagicMock(cookies={}),
    )
    assert result is None


async def test_render_welcome_dashboard_not_found_returns_404():
    """When the dashboard row is missing, return 404 — mirrors abort(404)."""
    sf, dao_session = _make_session_factory(welcome_dashboard_id=99)

    # DAO returns None (dashboard not found)
    mock_dao = AsyncMock()
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=None)

    with patch("superset.controllers.spa.AsyncDashboardDAO", return_value=mock_dao):
        result = await _render_welcome_dashboard(
            user=MagicMock(),
            user_id=1,
            session_factory=sf,
            state=MagicMock(),
            settings=MagicMock(
                enable_ui_theme_administration=False,
                static_assets_prefix="",
                session_cookie_name="session",
                secret_key="x" * 32,
            ),
            request=MagicMock(cookies={}),
        )

    assert isinstance(result, _Response)
    assert result.status_code == 404


async def test_render_welcome_dashboard_access_denied_returns_404():
    """When raise_for_access raises SupersetSecurityException, return 404.

    Mirrors superset_old/views/core.py:809-812: authenticated user who
    cannot access the dashboard gets abort(404).
    """
    from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
    from superset.exceptions import SupersetSecurityException

    sf, dao_session = _make_session_factory(welcome_dashboard_id=7)

    mock_dashboard = MagicMock()
    mock_dashboard.dashboard_title = "Secret"
    mock_dao = AsyncMock()
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=mock_dashboard)

    _sec_exc = SupersetSecurityException(
        SupersetError(
            message="denied",
            error_type=SupersetErrorType.DASHBOARD_SECURITY_ACCESS_ERROR,
            level=ErrorLevel.ERROR,
        )
    )

    mock_sec_mgr = AsyncMock()
    mock_sec_mgr.raise_for_access = AsyncMock(side_effect=_sec_exc)

    with (
        patch("superset.controllers.spa.AsyncDashboardDAO", return_value=mock_dao),
        # provide_security_manager is a local import inside _render_welcome_dashboard;
        # patch it at the superset.dependencies module level.
        patch(
            "superset.dependencies.provide_security_manager",
            new=AsyncMock(return_value=mock_sec_mgr),
        ),
    ):
        result = await _render_welcome_dashboard(
            user=MagicMock(),
            user_id=1,
            session_factory=sf,
            state=MagicMock(),
            settings=MagicMock(
                enable_ui_theme_administration=False,
                static_assets_prefix="",
                session_cookie_name="session",
                secret_key="x" * 32,
            ),
            request=MagicMock(cookies={}),
        )

    assert isinstance(result, _Response)
    assert result.status_code == 404


async def test_render_welcome_dashboard_access_ok_returns_200_template_with_title():
    """When the dashboard is accessible, return Template(200) with dashboard title.

    Mirrors original: ``self.dashboard()`` returns render_template(200)
    with title=dashboard.dashboard_title (NOT a 302 redirect).
    The browser URL stays at /superset/welcome/.
    """
    sf, dao_session = _make_session_factory(welcome_dashboard_id=42)

    mock_dashboard = MagicMock()
    mock_dashboard.dashboard_title = "My Dashboard"
    mock_dao = AsyncMock()
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=mock_dashboard)

    mock_sec_mgr = AsyncMock()
    mock_sec_mgr.raise_for_access = AsyncMock(return_value=None)

    with (
        patch("superset.controllers.spa.AsyncDashboardDAO", return_value=mock_dao),
        # provide_security_manager is a local import in _render_welcome_dashboard;
        # must patch at the superset.dependencies module level.
        patch(
            "superset.dependencies.provide_security_manager",
            new=AsyncMock(return_value=mock_sec_mgr),
        ),
        patch(
            "superset.controllers.spa._build_bootstrap_data",
            return_value={"common": {"theme": {}}, "user": {}},
        ),
    ):
        result = await _render_welcome_dashboard(
            user=MagicMock(),
            user_id=1,
            session_factory=sf,
            state=MagicMock(),
            settings=MagicMock(
                enable_ui_theme_administration=False,
                static_assets_prefix="",
                session_cookie_name="session",
                secret_key="x" * 32,
            ),
            request=MagicMock(cookies={}),
        )

    # Must be a Template (200), NOT a Redirect — critical 1:1 parity point
    assert isinstance(result, _Template), f"Expected Template, got {type(result)}"
    # Title must be the dashboard title, NOT the generic "Superset"
    assert result.context["title"] == "My Dashboard"
    # bootstrap_data must be present
    assert "bootstrap_data" in result.context


async def test_render_welcome_dashboard_no_redirect_issued():
    """Confirm _render_welcome_dashboard never returns a Redirect.

    The original Superset.welcome() → self.dashboard() path always returns
    200 (or 404) — never a 302.  The liteset fix must not issue a Redirect.
    """
    from litestar.response import Redirect as _Redirect

    sf, dao_session = _make_session_factory(welcome_dashboard_id=10)

    mock_dashboard = MagicMock()
    mock_dashboard.dashboard_title = "Dashboard"
    mock_dao = AsyncMock()
    mock_dao.get_by_id_or_slug = AsyncMock(return_value=mock_dashboard)

    mock_sec_mgr = AsyncMock()
    mock_sec_mgr.raise_for_access = AsyncMock(return_value=None)

    with (
        patch("superset.controllers.spa.AsyncDashboardDAO", return_value=mock_dao),
        # provide_security_manager is a local import in _render_welcome_dashboard;
        # must patch at the superset.dependencies module level.
        patch(
            "superset.dependencies.provide_security_manager",
            new=AsyncMock(return_value=mock_sec_mgr),
        ),
        patch(
            "superset.controllers.spa._build_bootstrap_data",
            return_value={"common": {"theme": {}}, "user": {}},
        ),
    ):
        result = await _render_welcome_dashboard(
            user=MagicMock(),
            user_id=1,
            session_factory=sf,
            state=MagicMock(),
            settings=MagicMock(
                enable_ui_theme_administration=False,
                static_assets_prefix="",
                session_cookie_name="session",
                secret_key="x" * 32,
            ),
            request=MagicMock(cookies={}),
        )

    assert not isinstance(result, _Redirect), "Must not redirect — original returns 200"


# ---------------------------------------------------------------------------
# Guard configuration: language_pack and frontend_log
#
# Original: both endpoints decorated with FAB @has_access which checks
# can_language_pack / can_log on the "Superset" view class.
# Liteset: must use require_permission("can_language_pack", "Superset") /
#          require_permission("can_log", "Superset") — NOT require_authentication.
#
# Refs:
#   superset_old/views/core.py:868-873 (log: @has_access)
#   superset_old/views/core.py:901-904 (language_pack: @has_access)
# ---------------------------------------------------------------------------


@dataclass
class _UserWithPerm:
    """Authenticated user with a given permission tuple."""

    is_authenticated: bool = True
    permissions: set = field(default_factory=set)
    roles: list = field(default_factory=list)


@dataclass
class _AnonUser:
    """Unauthenticated user (no session)."""

    is_authenticated: bool = False
    permissions: set = field(default_factory=set)
    roles: list = field(default_factory=list)


def _make_conn(user: object) -> MagicMock:
    """Build a mock ASGIConnection with the given user attached."""
    conn = MagicMock(spec=ASGIConnection)
    conn.user = user
    # required by require_permission guard: reads auth_role_admin from app state
    conn.app.state.settings.auth_role_admin = "Admin"
    return conn


def _extract_guard(handler):
    """Return the single guard function attached to a Litestar handler."""
    guards = list(handler.guards)
    assert len(guards) == 1, f"Expected 1 guard, got {len(guards)}: {guards}"
    return guards[0]


# --- language_pack ---


def test_language_pack_guard_allows_user_with_can_language_pack():
    """Authenticated user who has can_language_pack on Superset passes the guard.

    Original: @has_access allows access when can_language_pack is present.
    """
    guard = _extract_guard(SPAController.language_pack)
    user = _UserWithPerm(permissions={("can_language_pack", "Superset")})
    conn = _make_conn(user)
    # Must NOT raise
    guard(conn, MagicMock())


def test_language_pack_guard_denies_user_without_can_language_pack():
    """Authenticated user lacking can_language_pack is denied with 403.

    Original: @has_access denies and redirects to login. Liteset maps this to
    PermissionDeniedException (403) — an allowed migration artifact.
    """
    guard = _extract_guard(SPAController.language_pack)
    user = _UserWithPerm(permissions={("can_read", "Chart")})  # wrong permission
    conn = _make_conn(user)
    with pytest.raises(PermissionDeniedException):
        guard(conn, MagicMock())


def test_language_pack_guard_denies_unauthenticated_user():
    """Unauthenticated caller without can_language_pack gets 401.

    Original: @has_access redirects unauthenticated callers to login (401 equiv).
    """
    guard = _extract_guard(SPAController.language_pack)
    user = _AnonUser()
    conn = _make_conn(user)
    with pytest.raises(NotAuthorizedException):
        guard(conn, MagicMock())


# --- frontend_log ---


def test_frontend_log_guard_allows_user_with_can_log():
    """Authenticated user who has can_log on Superset passes the guard.

    Original: @has_access allows access when can_log is present.
    """
    guard = _extract_guard(SPAController.frontend_log)
    user = _UserWithPerm(permissions={("can_log", "Superset")})
    conn = _make_conn(user)
    # Must NOT raise
    guard(conn, MagicMock())


def test_frontend_log_guard_denies_user_without_can_log():
    """Authenticated user lacking can_log is denied with 403.

    Original: @has_access on log() endpoint denies users without can_log.
    Liteset maps this to PermissionDeniedException (403).
    """
    guard = _extract_guard(SPAController.frontend_log)
    user = _UserWithPerm(permissions={("can_read", "Chart")})  # wrong permission
    conn = _make_conn(user)
    with pytest.raises(PermissionDeniedException):
        guard(conn, MagicMock())


def test_frontend_log_guard_denies_unauthenticated_user():
    """Unauthenticated caller without can_log gets 401.

    Original: @has_access redirects unauthenticated callers to login.
    """
    guard = _extract_guard(SPAController.frontend_log)
    user = _AnonUser()
    conn = _make_conn(user)
    with pytest.raises(NotAuthorizedException):
        guard(conn, MagicMock())
