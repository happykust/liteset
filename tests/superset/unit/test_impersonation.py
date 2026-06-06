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
"""Engine-spec user impersonation (ported 1:1 from upstream).

Covers the base ``impersonate_user`` (URL username) + the Trino override
(``connect_args["user"]``) + the ``get_sync_engine`` / ``get_async_connection``
wiring that runs queries as the effective user.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from sqlalchemy.engine import make_url

from superset.db_engine_specs import get_engine_spec


def _spec(name: str) -> Any:
    return get_engine_spec(name, "")


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

        def get_extra(self) -> dict[str, Any]:
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

        def get_extra(self) -> dict[str, Any]:
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

        def get_extra(self) -> dict[str, Any]:
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
    local-part (1:1 upstream get_sqla_engine)."""
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

        def get_extra(self) -> dict[str, Any]:
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
