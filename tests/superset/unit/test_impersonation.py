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
"""Engine-spec user impersonation.

Covers the base ``impersonate_user`` (URL username) + the Trino override
(``connect_args["user"]``) + the ``get_sync_engine`` / ``get_async_connection``
wiring that runs queries as the effective user.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.engine import make_url

from superset.db_engine_specs import get_engine_spec


def _spec(name: str) -> Any:
    return get_engine_spec(name, "")


@pytest.fixture(autouse=True)
def _stub_trino_uri_validation(monkeypatch: Any) -> None:
    """``get_sync_engine`` validates the URI via
    ``db_engine_spec.validate_database_uri``, which calls
    ``url.get_driver_name()`` and would load the trino dialect — not installed
    in the test env.  Stub it so these impersonation tests exercise only the
    impersonation path, not real dialect loading.
    """
    monkeypatch.setattr(_spec("trino"), "validate_database_uri", lambda *a, **k: None)


def test_base_impersonate_user_sets_url_username() -> None:
    spec = _spec("postgresql")
    url = make_url("postgresql://svc@localhost:5432/db")
    new_url, kwargs = spec.impersonate_user(None, "alice", None, url, {})
    assert new_url.username == "alice"
    # base ``update_impersonation_config`` is a no-op → connect_args untouched
    assert kwargs.get("connect_args") == {}


def test_base_impersonate_user_none_username_noop() -> None:
    spec = _spec("postgresql")
    url = make_url("postgresql://svc@localhost:5432/db")
    new_url, _ = spec.impersonate_user(None, None, None, url, {})
    assert new_url.username == "svc"


def test_trino_impersonate_user_sets_connect_args() -> None:
    spec = _spec("trino")
    url = make_url("trino://svc@localhost:8085/tpch")
    new_url, kwargs = spec.impersonate_user(None, "alice", None, url, {})
    # Trino impersonates via connect_args["user"], NOT the URL username
    assert kwargs["connect_args"]["user"] == "alice"
    assert new_url.username == "svc"


def test_trino_impersonate_user_none_username_noop() -> None:
    spec = _spec("trino")
    url = make_url("trino://svc@localhost:8085/tpch")
    new_url, kwargs = spec.impersonate_user(None, None, None, url, {})
    assert kwargs.get("connect_args", {}) == {}
    assert new_url.username == "svc"


def test_get_sync_engine_applies_impersonation(monkeypatch: Any) -> None:
    """``get_sync_engine`` runs queries as the current request user."""
    import superset.utils.database as ud
    from superset.utils.core import get_username, set_current_user

    captured: dict[str, Any] = {}

    def _spy(uri: Any, **kw: Any) -> Any:
        captured["uri"] = str(uri)
        captured["connect_args"] = kw.get("connect_args")

        class _E:
            url = make_url(str(uri))

            def dispose(self) -> None:
                pass

        return _E()

    monkeypatch.setattr(ud, "create_engine", _spy)

    class _DB:
        sqlalchemy_uri = "trino://svc@localhost:8085/tpch"
        sqlalchemy_uri_decrypted = sqlalchemy_uri
        impersonate_user = True
        db_engine_spec = _spec("trino")

        def get_extra(self, source: Any = None) -> dict[str, Any]:
            return {}

        def get_effective_user(self, url: Any) -> str | None:
            return get_username() or (url.username if self.impersonate_user else None)

    class _User:
        username = "alice"

    set_current_user(_User())
    try:
        with ud.get_sync_engine(_DB()):
            pass
    finally:
        set_current_user(None)

    assert captured["connect_args"]["user"] == "alice"


def test_get_sync_engine_no_impersonation_when_disabled(monkeypatch: Any) -> None:
    import superset.utils.database as ud

    captured: dict[str, Any] = {}

    def _spy(uri: Any, **kw: Any) -> Any:
        captured["connect_args"] = kw.get("connect_args")

        class _E:
            url = make_url(str(uri))

            def dispose(self) -> None:
                pass

        return _E()

    monkeypatch.setattr(ud, "create_engine", _spy)

    class _DB:
        sqlalchemy_uri = "trino://svc@localhost:8085/tpch"
        sqlalchemy_uri_decrypted = sqlalchemy_uri
        impersonate_user = False
        db_engine_spec = _spec("trino")

        def get_extra(self, source: Any = None) -> dict[str, Any]:
            return {}

        def get_effective_user(self, url: Any) -> str | None:
            return url.username if self.impersonate_user else None

    with ud.get_sync_engine(_DB()):
        pass

    assert "user" not in (captured.get("connect_args") or {})


def test_get_sync_engine_preserves_password_when_impersonating(
    monkeypatch: Any,
) -> None:
    """Regression: the impersonation URL round-trip must NOT mask the password.

    ``str(URL)`` renders the password as ``***``; the wiring must use
    ``render_as_string(hide_password=False)`` so a password-bearing DB still
    authenticates under impersonation.
    """
    import superset.utils.database as ud
    from superset.utils.core import get_username, set_current_user

    captured: dict[str, Any] = {}

    def _spy(uri: Any, **kw: Any) -> Any:
        captured["uri"] = str(uri)

        class _E:
            url = make_url(str(uri))

            def dispose(self) -> None:
                pass

        return _E()

    monkeypatch.setattr(ud, "create_engine", _spy)

    class _DB:
        sqlalchemy_uri = "trino://svc:secretpass@localhost:8085/tpch"
        sqlalchemy_uri_decrypted = sqlalchemy_uri
        impersonate_user = True
        db_engine_spec = _spec("trino")

        def get_extra(self, source: Any = None) -> dict[str, Any]:
            return {}

        def get_effective_user(self, url: Any) -> str | None:
            return get_username() or (url.username if self.impersonate_user else None)

    class _User:
        username = "alice"

    set_current_user(_User())
    try:
        with ud.get_sync_engine(_DB()):
            pass
    finally:
        set_current_user(None)

    assert "secretpass" in captured["uri"]
    assert "***" not in captured["uri"]


def test_impersonate_with_email_prefix(monkeypatch: Any) -> None:
    """IMPERSONATE_WITH_EMAIL_PREFIX rewrites the effective user to the email
    local-part."""
    import superset.utils.database as ud
    from superset.utils.core import get_username, set_current_user
    from superset.utils.feature_flags import feature_flag_manager

    captured: dict[str, Any] = {}

    def _spy(uri: Any, **kw: Any) -> Any:
        captured["connect_args"] = kw.get("connect_args")

        class _E:
            url = make_url(str(uri))

            def dispose(self) -> None:
                pass

        return _E()

    monkeypatch.setattr(ud, "create_engine", _spy)

    class _DB:
        sqlalchemy_uri = "trino://svc@localhost:8085/tpch"
        sqlalchemy_uri_decrypted = sqlalchemy_uri
        impersonate_user = True
        db_engine_spec = _spec("trino")

        def get_extra(self, source: Any = None) -> dict[str, Any]:
            return {}

        def get_effective_user(self, url: Any) -> str | None:
            return get_username() or (url.username if self.impersonate_user else None)

    class _User:
        username = "alice"
        email = "bob.smith@corp.com"

    set_current_user(_User())
    try:
        feature_flag_manager.init_from_config({"IMPERSONATE_WITH_EMAIL_PREFIX": False})
        with ud.get_sync_engine(_DB()):
            pass
        assert captured["connect_args"]["user"] == "alice"

        feature_flag_manager.init_from_config({"IMPERSONATE_WITH_EMAIL_PREFIX": True})
        with ud.get_sync_engine(_DB()):
            pass
        assert captured["connect_args"]["user"] == "bob.smith"
    finally:
        feature_flag_manager.init_from_config({"IMPERSONATE_WITH_EMAIL_PREFIX": False})
        set_current_user(None)


# --- OAuth2 access-token resolution for impersonation (#3) ---------------------


def _token(access: str | None, exp_delta_h: int, refresh: str | None):
    from datetime import datetime, timedelta

    t = MagicMock()
    t.access_token = access
    t.access_token_expiration = datetime.now() + timedelta(hours=exp_delta_h)
    t.refresh_token = refresh
    return t


def _sync_session_cm(one_or_none_value):
    sess = MagicMock()
    sess.query.return_value.filter_by.return_value.one_or_none.return_value = (
        one_or_none_value
    )
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=sess)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, sess


def test_sync_oauth2_token_valid_returned() -> None:
    from unittest.mock import patch

    from superset.utils.oauth2 import sync_get_oauth2_access_token

    cm, _ = _sync_session_cm(_token("TOK123", +1, None))
    with patch("superset.db.session.get_sync_session", return_value=cm):
        assert sync_get_oauth2_access_token({}, 5, 1, MagicMock()) == "TOK123"


def test_sync_oauth2_token_expired_no_refresh_deletes() -> None:
    from unittest.mock import patch

    from superset.utils.oauth2 import sync_get_oauth2_access_token

    cm, sess = _sync_session_cm(_token("OLD", -1, None))
    with patch("superset.db.session.get_sync_session", return_value=cm):
        assert sync_get_oauth2_access_token({}, 5, 1, MagicMock()) is None
    assert sess.delete.called


def test_sync_oauth2_token_missing_returns_none() -> None:
    from unittest.mock import patch

    from superset.utils.oauth2 import sync_get_oauth2_access_token

    cm, _ = _sync_session_cm(None)
    with patch("superset.db.session.get_sync_session", return_value=cm):
        assert sync_get_oauth2_access_token({}, 5, 1, MagicMock()) is None


def test_sync_oauth2_resolver_gating() -> None:
    """_sync_oauth2_access_token only resolves when oauth2-config + a user exist."""
    from unittest.mock import patch

    import superset.utils.database as ud
    from superset.utils.core import set_current_user

    spec = _spec("trino")

    class _NonOAuthDB:
        id = 1

        def get_oauth2_config(self):
            return None

    # No OAuth2 config → None (no resolver call).
    assert ud._sync_oauth2_access_token(_NonOAuthDB(), spec) is None

    class _OAuthDB:
        id = 2

        def get_oauth2_config(self):
            return {"id": "client"}

    class _User:
        id = 1
        username = "alice"

    # OAuth2 config + bound user → delegates to sync_get_oauth2_access_token.
    set_current_user(_User())
    try:
        with patch(
            "superset.utils.oauth2.sync_get_oauth2_access_token",
            return_value="BEARER",
        ):
            assert ud._sync_oauth2_access_token(_OAuthDB(), spec) == "BEARER"
    finally:
        set_current_user(None)

    # OAuth2 config but no bound user → None.
    assert ud._sync_oauth2_access_token(_OAuthDB(), spec) is None


# --- Sync OAuth2 token refresh (the new sync path) -----------------------------


def test_get_oauth2_fresh_token_sync_posts_and_returns_body(monkeypatch: Any) -> None:
    """``get_oauth2_fresh_token_sync`` POSTs the refresh body + returns the JSON."""
    captured: dict[str, Any] = {}

    class _Resp:
        def json(self) -> dict[str, Any]:
            return {"access_token": "NEW", "expires_in": 3600}

    class _Client:
        def __init__(self, timeout: Any = None) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def post(self, uri: str, **kw: Any) -> _Resp:
            captured["uri"] = uri
            captured["kw"] = kw
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)

    spec = _spec("trino")
    config = {
        "id": "client-id",
        "secret": "client-secret",
        "token_request_uri": "https://idp/token",
        "request_content_type": "json",
    }
    out = spec.get_oauth2_fresh_token_sync(config, "REFRESH123")

    assert out == {"access_token": "NEW", "expires_in": 3600}
    assert captured["uri"] == "https://idp/token"
    # json body (request_content_type defaults to json, not data)
    assert captured["kw"]["json"] == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "REFRESH123",
        "grant_type": "refresh_token",
    }
    assert "data" not in captured["kw"]


def test_get_oauth2_fresh_token_sync_data_content_type(monkeypatch: Any) -> None:
    """``request_content_type == "data"`` sends a form body, not JSON."""
    captured: dict[str, Any] = {}

    class _Resp:
        def json(self) -> dict[str, Any]:
            return {"access_token": "NEW"}

    class _Client:
        def __init__(self, timeout: Any = None) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def post(self, uri: str, **kw: Any) -> _Resp:
            captured["kw"] = kw
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)

    spec = _spec("trino")
    config = {
        "id": "i",
        "secret": "s",
        "token_request_uri": "https://idp/token",
        "request_content_type": "data",
    }
    spec.get_oauth2_fresh_token_sync(config, "R")
    assert "data" in captured["kw"]
    assert "json" not in captured["kw"]


def test_sync_oauth2_token_expired_refresh_returns_new_and_persists() -> None:
    """Expired token + refresh_token → refresh → new token, row updated."""
    import uuid
    from contextlib import contextmanager
    from unittest.mock import patch

    from superset.utils.oauth2 import sync_get_oauth2_access_token

    token = _token("OLD", -1, "REFRESH123")
    cm, sess = _sync_session_cm(token)

    uuid_stub = uuid.uuid4()

    # The sync lock would otherwise open a second get_sync_session; stub it.
    @contextmanager
    def _noop_lock(namespace: str, **kwargs: Any):
        yield uuid_stub

    spec = MagicMock()
    spec.get_oauth2_fresh_token_sync.return_value = {
        "access_token": "NEW",
        "expires_in": 3600,
    }

    with (
        patch("superset.db.session.get_sync_session", return_value=cm),
        patch(
            "superset.distributed_lock.sync_key_value_distributed_lock",
            _noop_lock,
        ),
    ):
        result = sync_get_oauth2_access_token({"id": "x"}, 5, 1, spec)

    assert result == "NEW"
    # The stored token row was mutated + committed in the same session.
    assert token.access_token == "NEW"
    spec.get_oauth2_fresh_token_sync.assert_called_once()
    assert sess.add.called
    assert sess.commit.called


def test_sync_oauth2_token_refresh_revoked_returns_none() -> None:
    """Refresh response without ``access_token`` (revoked) → None."""
    from contextlib import contextmanager
    from unittest.mock import patch

    from superset.utils.oauth2 import sync_get_oauth2_access_token

    token = _token("OLD", -1, "REFRESH123")
    cm, _ = _sync_session_cm(token)

    @contextmanager
    def _noop_lock(namespace: str, **kwargs: Any):
        yield None

    spec = MagicMock()
    spec.get_oauth2_fresh_token_sync.return_value = {"error": "invalid_grant"}

    with (
        patch("superset.db.session.get_sync_session", return_value=cm),
        patch(
            "superset.distributed_lock.sync_key_value_distributed_lock",
            _noop_lock,
        ),
    ):
        assert sync_get_oauth2_access_token({"id": "x"}, 5, 1, spec) is None


# --- Sync distributed lock -----------------------------------------------------


def _lock_session_cm(existing_on_acquire, existing_on_release=None):
    """Build a sync-session context manager for the lock.

    ``session.query(...).filter(...).one_or_none()`` is called twice: once on
    acquire (contention check) and once on release (fetch-to-delete). Return
    the two values in sequence.
    """
    if existing_on_release is None:
        existing_on_release = MagicMock()
    sess = MagicMock()
    one_or_none = sess.query.return_value.filter.return_value.one_or_none
    one_or_none.side_effect = [existing_on_acquire, existing_on_release]
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=sess)
    cm.__exit__ = MagicMock(return_value=False)
    return cm, sess


def test_sync_lock_acquires_and_releases() -> None:
    from unittest.mock import patch

    from superset.distributed_lock import sync_key_value_distributed_lock
    from superset.distributed_lock.utils import get_key

    release_row = MagicMock()
    cm, sess = _lock_session_cm(None, release_row)
    with patch("superset.db.session.get_sync_session", return_value=cm):
        with sync_key_value_distributed_lock("refresh_oauth2_token", user_id=1) as key:
            assert key == get_key("refresh_oauth2_token", user_id=1)
            # acquired: a row was added + committed
            assert sess.add.called
    # released: the row was deleted on exit
    sess.delete.assert_called_once_with(release_row)


def test_sync_lock_already_taken_raises() -> None:
    from unittest.mock import patch

    from superset.distributed_lock import sync_key_value_distributed_lock
    from superset.exceptions import CreateKeyValueDistributedLockFailedException

    cm, sess = _lock_session_cm(MagicMock())  # contended on acquire
    with patch("superset.db.session.get_sync_session", return_value=cm):
        with pytest.raises(CreateKeyValueDistributedLockFailedException):
            with sync_key_value_distributed_lock("refresh_oauth2_token", user_id=1):
                pass
    # never acquired → never added
    assert not sess.add.called
