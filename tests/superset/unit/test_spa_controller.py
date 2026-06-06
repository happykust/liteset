import json as _json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar import Litestar
from litestar.testing import AsyncTestClient

from superset.app import create_app
from superset.config import SupersetSettings
from superset.controllers.spa import SPA_ROUTE_PREFIXES, SPAController


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

    assert result == {"status": "OK"}
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

    assert result == {"status": "OK"}


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
