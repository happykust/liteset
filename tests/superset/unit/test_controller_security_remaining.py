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
from __future__ import annotations

import jwt

from superset.controllers.security import GuestTokenUser, SecurityController
from superset.security.guest import create_guest_access_token, GuestUser


def test_controller_path() -> None:
    assert SecurityController.path == "/api/v1/security"


def test_controller_has_csrf_endpoint() -> None:
    assert hasattr(SecurityController, "csrf_token")


def test_controller_has_guest_token_endpoint() -> None:
    assert hasattr(SecurityController, "guest_token")


def test_controller_has_search_roles_endpoint() -> None:
    assert hasattr(SecurityController, "search_roles")


class TestGuestTokenUserSparseDict:
    """GuestTokenUser._to_sparse_dict() must match the original Marshmallow
    UserSchema behaviour: only explicitly-provided (non-None) fields appear.

    Original evidence: UserSchema().load({}) == {} and
    UserSchema().load({"first_name": "John"}) == {"first_name": "John"}.
    """

    def test_all_none_produces_empty_dict(self) -> None:
        user = GuestTokenUser()
        assert user._to_sparse_dict() == {}

    def test_only_username_provided(self) -> None:
        user = GuestTokenUser(username="testuser")
        assert user._to_sparse_dict() == {"username": "testuser"}

    def test_all_fields_provided(self) -> None:
        user = GuestTokenUser(username="alice", first_name="Alice", last_name="Smith")
        assert user._to_sparse_dict() == {
            "username": "alice",
            "first_name": "Alice",
            "last_name": "Smith",
        }

    def test_empty_string_is_included(self) -> None:
        """An explicitly-provided empty string is included (caller sent ''),
        unlike None which is excluded. Marshmallow also includes
        explicitly-provided empty strings.
        """
        user = GuestTokenUser(username="", first_name="Alice", last_name="Smith")
        assert user._to_sparse_dict() == {
            "username": "",
            "first_name": "Alice",
            "last_name": "Smith",
        }

    def test_partial_fields_only_those_in_output(self) -> None:
        user = GuestTokenUser(first_name="Bob")
        result = user._to_sparse_dict()
        assert "username" not in result
        assert result["first_name"] == "Bob"
        assert "last_name" not in result


_SECRET = "test-secret-at-least-32-bytes-long!"


class TestGuestTokenJwtRoundTrip:
    """The sparse user dict stored in the JWT must cause GuestUser.from_token_payload
    to apply the correct canonical fallbacks:

      self.username = user.get("username", "guest_user")
      self.first_name = user.get("first_name", "Guest")
      self.last_name = user.get("last_name", "User")
    """

    def _encode_and_decode(self, user_dict: dict) -> GuestUser:
        token_str = create_guest_access_token(
            secret_key=_SECRET,
            user=user_dict,
            resources=[],
            rls=[],
        )
        payload = jwt.decode(token_str, _SECRET, algorithms=["HS256"])
        return GuestUser.from_token_payload(payload)

    def test_empty_user_dict_gets_canonical_fallbacks(self) -> None:
        guest = self._encode_and_decode({})
        assert guest.username == "guest_user"
        assert guest.first_name == "Guest"
        assert guest.last_name == "User"

    def test_only_username_provided_first_last_fallback(self) -> None:
        guest = self._encode_and_decode({"username": "embed_user"})
        assert guest.username == "embed_user"
        assert guest.first_name == "Guest"
        assert guest.last_name == "User"

    def test_all_fields_provided_no_fallback(self) -> None:
        guest = self._encode_and_decode(
            {"username": "alice", "first_name": "Alice", "last_name": "Smith"}
        )
        assert guest.username == "alice"
        assert guest.first_name == "Alice"
        assert guest.last_name == "Smith"

    def test_guest_token_user_to_sparse_dict_empty_produces_fallbacks(self) -> None:
        """The full path: GuestTokenUser() → sparse dict → JWT → GuestUser
        with canonical fallbacks.  This is the regression test for the
        bug where GuestTokenUser(username='', ...) caused username='' instead
        of the canonical 'guest_user' fallback."""
        user = GuestTokenUser()
        sparse = user._to_sparse_dict()
        assert sparse == {}
        guest = self._encode_and_decode(sparse)
        assert guest.username == "guest_user"
        assert guest.first_name == "Guest"
        assert guest.last_name == "User"

    def test_guest_token_user_partial_to_sparse_dict(self) -> None:
        user = GuestTokenUser(username="embed")
        sparse = user._to_sparse_dict()
        assert sparse == {"username": "embed"}
        guest = self._encode_and_decode(sparse)
        assert guest.username == "embed"
        assert guest.first_name == "Guest"
        assert guest.last_name == "User"
