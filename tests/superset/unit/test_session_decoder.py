"""Tests for FlaskSessionDecoder — itsdangerous cookie decoding.

Cookies are minted with Flask's exact signer configuration
(``key_derivation="hmac"`` + SHA-1 + ``TaggedJSONSerializer``) — the
itsdangerous *defaults* (django-concat) produce incompatible signatures,
which is precisely the round-10 regression this file guards against.
"""

from __future__ import annotations

import hashlib

from flask.json.tag import TaggedJSONSerializer
from itsdangerous import URLSafeTimedSerializer

from superset.security.session_decoder import FlaskSessionDecoder

SECRET_KEY = "test-secret-key-at-least-16-chars"


def _create_flask_session_cookie(
    data: dict,
    secret_key: str = SECRET_KEY,
    salt: str = "cookie-session",
) -> str:
    """Create a real Flask-format signed session cookie for testing."""
    s = URLSafeTimedSerializer(
        secret_key,
        salt=salt,
        serializer=TaggedJSONSerializer(),
        signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1},
    )
    return s.dumps(data)


def test_decode_real_flask_session_interface_cookie():
    """Regression: a cookie minted by Flask itself must decode.

    Pre-fix the decoder used itsdangerous' default key derivation
    (django-concat) → BadSignature on every real Flask cookie →
    get_user_id() always None (Strangler-Fig session auth dead).
    """
    import flask

    app = flask.Flask(__name__)
    app.secret_key = SECRET_KEY
    signer = flask.sessions.SecureCookieSessionInterface().get_signing_serializer(app)
    cookie = signer.dumps({"_user_id": "5", "csrf_token": "tok"})

    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY)
    assert decoder.get_user_id(cookie) == 5


def test_decode_valid_cookie():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY)
    cookie = _create_flask_session_cookie({"_user_id": "42", "csrf_token": "abc123"})
    payload = decoder.decode(cookie)
    assert payload is not None
    assert payload["_user_id"] == "42"


def test_decode_extracts_user_id():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY)
    cookie = _create_flask_session_cookie({"_user_id": "7"})
    user_id = decoder.get_user_id(cookie)
    assert user_id == 7


def test_decode_invalid_signature_returns_none():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY)
    cookie = _create_flask_session_cookie(
        {"_user_id": "1"}, secret_key="wrong-secret-key-at-least-32-bytes!"
    )
    payload = decoder.decode(cookie)
    assert payload is None


def test_decode_tampered_cookie_returns_none():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY)
    cookie = _create_flask_session_cookie({"_user_id": "1"})
    tampered = cookie[:-5] + "XXXXX"
    payload = decoder.decode(tampered)
    assert payload is None


def test_decode_empty_cookie_returns_none():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY)
    payload = decoder.decode("")
    assert payload is None


def test_decode_none_cookie_returns_none():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY)
    payload = decoder.decode(None)
    assert payload is None


def test_get_user_id_no_user_id_in_payload():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY)
    cookie = _create_flask_session_cookie({"other_key": "value"})
    user_id = decoder.get_user_id(cookie)
    assert user_id is None


def test_get_user_id_invalid_cookie():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY)
    user_id = decoder.get_user_id("garbage-data")
    assert user_id is None


def test_decode_with_max_age():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY, max_age=3600)
    cookie = _create_flask_session_cookie({"_user_id": "1"})
    payload = decoder.decode(cookie)
    assert payload is not None


def test_decode_with_custom_salt():
    custom_salt = "custom-salt"
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY, salt=custom_salt)
    cookie = _create_flask_session_cookie({"_user_id": "1"}, salt=custom_salt)
    payload = decoder.decode(cookie)
    assert payload is not None
    assert payload["_user_id"] == "1"


def test_decode_with_mismatched_salt_returns_none():
    decoder = FlaskSessionDecoder(secret_key=SECRET_KEY, salt="wrong-salt")
    cookie = _create_flask_session_cookie({"_user_id": "1"})
    payload = decoder.decode(cookie)
    assert payload is None
