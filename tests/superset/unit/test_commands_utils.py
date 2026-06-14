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
"""Port of ``tests/unit_tests/commands/test_utils.py``.

Adapted to the Liteset async port of ``superset.commands.utils``:

* ``populate_owner_list`` / ``compute_owner_list`` / ``validate_tags`` are
  async and take the security manager and acting user explicitly instead of
  reading Flask ``g`` / ``current_user`` / ``get_user_id``.  The current user
  is resolved through ``security_manager.find_user_by_id`` and admin status
  through ``security_manager.is_admin(user)``.
* ``can_access`` is an async call taking ``user=`` as a keyword argument.
* ``update_tags`` / ``validate_tags`` instantiate ``AsyncTagDAO`` (imported
  from ``superset.db.daos.tag``) and pass the Enum *name* (string) for the
  object type, so the DAO assertions use ``object_type.name``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call, MagicMock, patch

import pytest

from superset.commands.utils import (
    compute_owner_list,
    populate_owner_list,
    update_tags,
    validate_tags,
)
from superset.exceptions import TagForbiddenError, TagNotFoundValidationError
from superset.models.security import User
from superset.tags.models import ObjectType, Tag, TagType

OBJECT_TYPES = {ObjectType.chart, ObjectType.chart}
MOCK_TAGS = [
    Tag(
        id=1,
        name="first",
        type=TagType.custom,
    ),
    Tag(
        id=2,
        name="second",
        type=TagType.custom,
    ),
    Tag(
        id=3,
        name="third",
        type=TagType.custom,
    ),
    Tag(
        id=4,
        name="type:dashboard",
        type=TagType.type,
    ),
    Tag(
        id=4,
        name="owner:1",
        type=TagType.owner,
    ),
    Tag(
        id=4,
        name="avorited_by:2",
        type=TagType.favorited_by,
    ),
]


def _security_manager(
    current_user: User | None = None,
    is_admin: bool = False,
    resolved_users: dict[int, User] | None = None,
) -> MagicMock:
    """Build a mocked async security manager for owner-list tests.

    ``find_user_by_id`` resolves the current user id to ``current_user`` and
    any other id via ``resolved_users``; ``is_admin`` is a sync method.
    """
    resolved_users = resolved_users or {}
    sm = MagicMock()

    async def _find_user_by_id(user_id: int) -> User | None:
        if current_user is not None and user_id == current_user.id:
            return current_user
        return resolved_users.get(user_id)

    sm.find_user_by_id = AsyncMock(side_effect=_find_user_by_id)
    sm.is_admin = MagicMock(return_value=is_admin)
    return sm


async def test_populate_owner_list_default_to_user():
    """
    Test the ``populate_owner_list`` method when no owners are provided
    and default_to_user is True (non-admin).
    """
    current_user = User(id=4, first_name="Current", last_name="User")
    sm = _security_manager(current_user=current_user, is_admin=False)
    owner_list = await populate_owner_list(
        sm, current_user.id, [], default_to_user=True
    )
    assert owner_list == [current_user]


async def test_populate_owner_list_default_to_user_handle_none():
    """
    Test the ``populate_owner_list`` method when owners is None
    and default_to_user is True (non-admin).
    """
    current_user = User(id=4, first_name="Current", last_name="User")
    sm = _security_manager(current_user=current_user, is_admin=False)
    owner_list = await populate_owner_list(
        sm, current_user.id, None, default_to_user=True
    )
    assert owner_list == [current_user]


async def test_populate_owner_list_admin_user():
    """
    Test the ``populate_owner_list`` method when an admin is setting
    another user as an owner and default_to_user is False.
    """
    test_user = User(id=1, first_name="First", last_name="Last")
    admin_user = User(id=4, first_name="Admin", last_name="User")
    sm = _security_manager(
        current_user=admin_user, is_admin=True, resolved_users={1: test_user}
    )

    owner_list = await populate_owner_list(
        sm, admin_user.id, [1], default_to_user=False
    )
    assert owner_list == [test_user]


async def test_populate_owner_list_admin_user_empty_list():
    """
    Test the ``populate_owner_list`` method when an admin is setting an empty list
    of owners.
    """
    admin_user = User(id=4, first_name="Admin", last_name="User")
    sm = _security_manager(current_user=admin_user, is_admin=True)
    owner_list = await populate_owner_list(sm, admin_user.id, [], default_to_user=False)
    assert owner_list == []


async def test_populate_owner_list_non_admin():
    """
    Test the ``populate_owner_list`` method when a non admin is adding
    another user as an owner and default_to_user is False (both get added).
    """
    test_user = User(id=1, first_name="First", last_name="Last")
    non_admin = User(id=4, first_name="Non", last_name="admin")
    sm = _security_manager(
        current_user=non_admin, is_admin=False, resolved_users={1: test_user}
    )

    owner_list = await populate_owner_list(sm, non_admin.id, [1], default_to_user=False)
    assert owner_list == [non_admin, test_user]


@patch("superset.commands.utils.populate_owner_list")
async def test_compute_owner_list_new_owners(mock_populate_owner_list):
    """
    Test the ``compute_owner_list`` method when replacing the owner list.
    """
    mock_populate_owner_list.return_value = []
    sm = MagicMock()
    current_owners = [User(id=1), User(id=2), User(id=3)]
    new_owners = [4, 5, 6]

    await compute_owner_list(sm, None, current_owners, new_owners)
    mock_populate_owner_list.assert_called_once_with(
        sm, None, new_owners, default_to_user=False
    )


@patch("superset.commands.utils.populate_owner_list")
async def test_compute_owner_list_no_new_owners(mock_populate_owner_list):
    """
    Test the ``compute_owner_list`` method when replacing new_owners is None.
    """
    mock_populate_owner_list.return_value = []
    sm = MagicMock()
    current_owners = [User(id=1), User(id=2), User(id=3)]
    new_owners = None

    await compute_owner_list(sm, None, current_owners, new_owners)
    mock_populate_owner_list.assert_called_once_with(
        sm, None, [1, 2, 3], default_to_user=False
    )


@patch("superset.commands.utils.populate_owner_list")
async def test_compute_owner_list_new_owner_empty_list(mock_populate_owner_list):
    """
    Test the ``compute_owner_list`` method when new_owners is an empty list.
    """
    mock_populate_owner_list.return_value = []
    sm = MagicMock()
    current_owners = [User(id=1), User(id=2), User(id=3)]
    new_owners = []

    await compute_owner_list(sm, None, current_owners, new_owners)
    mock_populate_owner_list.assert_called_once_with(
        sm, None, new_owners, default_to_user=False
    )


@patch("superset.commands.utils.populate_owner_list")
async def test_compute_owner_list_no_owners(mock_populate_owner_list):
    """
    Test the ``compute_owner_list`` method when current ownership is an empty list.
    """
    mock_populate_owner_list.return_value = []
    sm = MagicMock()
    current_owners = []
    new_owners = [4, 5, 6]

    await compute_owner_list(sm, None, current_owners, new_owners)
    mock_populate_owner_list.assert_called_once_with(
        sm, None, new_owners, default_to_user=False
    )


@patch("superset.commands.utils.populate_owner_list")
async def test_compute_owner_list_no_owners_handle_none(mock_populate_owner_list):
    """
    Test the ``compute_owner_list`` method when current ownership is None.
    """
    mock_populate_owner_list.return_value = []
    sm = MagicMock()
    current_owners = None
    new_owners = [4, 5, 6]

    await compute_owner_list(sm, None, current_owners, new_owners)
    mock_populate_owner_list.assert_called_once_with(
        sm, None, new_owners, default_to_user=False
    )


def _validate_tags_sm() -> MagicMock:
    """Async security manager mock with a session-bearing dao."""
    sm = MagicMock()
    sm.dao = MagicMock()
    sm.dao.session = MagicMock()
    return sm


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
async def test_validate_tags_new_tags_is_none(object_type):
    """
    Test the ``validate_tags`` method when new_tags is None.
    """
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock()
    user = MagicMock()
    await validate_tags(object_type, MOCK_TAGS, None, sm, user)
    sm.can_access.assert_not_called()


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
async def test_validate_tags_empty_list_can_write_on_tag(object_type):
    """
    Test the ``validate_tags`` method when new_tags is an empty list and
    user has permission to write on tag.
    """
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock(return_value=True)
    user = MagicMock()
    await validate_tags(object_type, MOCK_TAGS, [], sm, user)
    sm.can_access.assert_called_once_with("can_write", "Tag", user=user)


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
async def test_validate_tags_empty_list_can_tag_on_object(object_type):
    """
    Test the ``validate_tags`` method when new_tags is an empty list and
    user has permission to tag objects.
    """
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock(side_effect=[False, True])
    user = MagicMock()
    await validate_tags(object_type, MOCK_TAGS, [], sm, user)
    sm.can_access.assert_has_calls(
        [
            call("can_write", "Tag", user=user),
            call("can_tag", object_type.name.capitalize(), user=user),
        ]
    )


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
async def test_validate_tags_empty_list_missing_permission(object_type):
    """
    Test the ``validate_tags`` method when new_tags is an empty list and
    the user doesn't have the required permission.
    """
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock(side_effect=[False, False])
    user = MagicMock()
    with pytest.raises(TagForbiddenError):
        await validate_tags(object_type, MOCK_TAGS, [], sm, user)
    sm.can_access.assert_has_calls(
        [
            call("can_write", "Tag", user=user),
            call("can_tag", object_type.name.capitalize(), user=user),
        ]
    )


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
async def test_validate_tags_no_changes_can_write_on_tag(object_type):
    """
    Test the ``validate_tags`` method when new_tags is equal to existing tags
    and user has permission to write on tag.
    """
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock()
    user = MagicMock()
    new_tags = [tag.id for tag in MOCK_TAGS if tag.type == TagType.custom]
    await validate_tags(object_type, MOCK_TAGS, new_tags, sm, user)
    sm.can_access.assert_not_called()


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
async def test_validate_tags_no_changes_can_tag_on_object(object_type):
    """
    Test the ``validate_tags`` method when new_tags is equal to existing tags
    and user has permission to tag objects.
    """
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock()
    user = MagicMock()
    new_tags = [tag.id for tag in MOCK_TAGS if tag.type == TagType.custom]
    await validate_tags(object_type, MOCK_TAGS, new_tags, sm, user)
    sm.can_access.assert_not_called()


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
async def test_validate_tags_no_changes_missing_permission(object_type):
    """
    Test the ``validate_tags`` method when new_tags is equal to existing tags
    the user doens't have the required perms.
    """
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock()
    user = MagicMock()
    new_tags = [tag.id for tag in MOCK_TAGS if tag.type == TagType.custom]
    await validate_tags(object_type, MOCK_TAGS, new_tags, sm, user)
    sm.can_access.assert_not_called()


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
@patch("superset.db.daos.tag.AsyncTagDAO")
async def test_validate_tags_add_new_tags_can_write_on_tag(
    mock_tag_dao_cls, object_type
):
    """
    Test the ``validate_tags`` method when new_tags are added and user has
    permission to write on tag.
    """
    new_tag_ids = [tag.id for tag in MOCK_TAGS if tag.type == TagType.custom]
    new_tag = {
        "id": 10,
        "name": "New test tag",
        "type": TagType.custom,
    }
    new_tag_ids.append(new_tag["id"])

    mock_tag_dao_cls.return_value.find_by_id = AsyncMock(return_value=new_tag)
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock(return_value=True)
    user = MagicMock()

    await validate_tags(object_type, MOCK_TAGS, new_tag_ids, sm, user)

    sm.can_access.assert_called_once_with("can_write", "Tag", user=user)


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
@patch("superset.db.daos.tag.AsyncTagDAO")
async def test_validate_tags_add_new_tags_can_tag_on_object(
    mock_tag_dao_cls, object_type
):
    """
    Test the ``validate_tags`` method when new_tags are added and user has
    permission to tag objects.
    """
    current_tags = [tag for tag in MOCK_TAGS if tag.type == TagType.custom]
    new_tag = current_tags.pop()
    new_tag_ids = [tag.id for tag in current_tags]
    new_tag_ids.append(new_tag.id)

    mock_tag_dao_cls.return_value.find_by_id = AsyncMock(return_value=new_tag)
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock(side_effect=[False, True])
    user = MagicMock()

    await validate_tags(object_type, current_tags, new_tag_ids, sm, user)

    sm.can_access.assert_has_calls(
        [
            call("can_write", "Tag", user=user),
            call("can_tag", object_type.name.capitalize(), user=user),
        ]
    )


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
@patch("superset.db.daos.tag.AsyncTagDAO")
async def test_validate_tags_can_write_on_tag_unable_to_find_tag(
    mock_tag_dao_cls, object_type
):
    """
    Test the ``validate_tags`` method when an un-existing tag is being
    added and user has permission to write on tag.
    """
    fake_id = 100
    mock_tag_dao_cls.return_value.find_by_id = AsyncMock(return_value=None)
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock(return_value=True)
    user = MagicMock()
    with pytest.raises(TagNotFoundValidationError):
        await validate_tags(object_type, MOCK_TAGS, [fake_id], sm, user)
    sm.can_access.assert_called_once_with("can_write", "Tag", user=user)


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
@patch("superset.db.daos.tag.AsyncTagDAO")
async def test_validate_tags_can_tag_on_object_unable_to_find_tag(
    mock_tag_dao_cls, object_type
):
    """
    Test the ``validate_tags`` method when an un-existing tag is being
    added and user has permission to tag on object.
    """
    fake_id = 100
    mock_tag_dao_cls.return_value.find_by_id = AsyncMock(return_value=None)
    sm = _validate_tags_sm()
    sm.can_access = AsyncMock(side_effect=[False, True])
    user = MagicMock()
    with pytest.raises(TagNotFoundValidationError):
        await validate_tags(object_type, MOCK_TAGS, [fake_id], sm, user)
    sm.can_access.assert_has_calls(
        [
            call("can_write", "Tag", user=user),
            call("can_tag", object_type.name.capitalize(), user=user),
        ]
    )


def _update_tags_dao(mock_tag_dao_cls) -> MagicMock:
    """Configure the patched ``AsyncTagDAO`` with async CRUD methods."""
    dao = mock_tag_dao_cls.return_value
    dao.find_by_ids = AsyncMock(return_value=[])
    dao.delete_tagged_object = AsyncMock()
    dao.create_custom_tagged_objects = AsyncMock()
    return dao


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
@patch("superset.db.daos.tag.AsyncTagDAO")
async def test_update_tags_adding_tags(mock_tag_dao_cls, object_type):
    """
    Test the ``update_tags`` method when adding tags.
    """
    dao = _update_tags_dao(mock_tag_dao_cls)
    current_tags = [tag for tag in MOCK_TAGS if tag.type == TagType.custom]
    new_tag = current_tags.pop()
    new_tags = [tag for tag in MOCK_TAGS if tag.type == TagType.custom]
    new_tag_ids = [tag.id for tag in new_tags]

    dao.find_by_ids = AsyncMock(return_value=[new_tag])

    await update_tags(object_type, 1, current_tags, new_tag_ids, MagicMock())

    dao.find_by_ids.assert_called_once_with([new_tag.id])
    dao.delete_tagged_object.assert_not_called()
    dao.create_custom_tagged_objects.assert_called_once_with(
        object_type.name, 1, [new_tag.name]
    )


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
@patch("superset.db.daos.tag.AsyncTagDAO")
async def test_update_tags_removing_tags(mock_tag_dao_cls, object_type):
    """
    Test the ``update_tags`` method when removing existing tags.
    """
    dao = _update_tags_dao(mock_tag_dao_cls)
    new_tags = [tag for tag in MOCK_TAGS if tag.type == TagType.custom]
    tag_to_be_removed = new_tags.pop()
    new_tag_ids = [tag.id for tag in new_tags]

    await update_tags(object_type, 1, MOCK_TAGS, new_tag_ids, MagicMock())

    dao.delete_tagged_object.assert_called_once_with(
        object_type.name, 1, tag_to_be_removed.name
    )
    dao.create_custom_tagged_objects.assert_not_called()


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
@patch("superset.db.daos.tag.AsyncTagDAO")
async def test_update_tags_adding_and_removing_tags(mock_tag_dao_cls, object_type):
    """
    Test the ``update_tags`` method when adding and removing existing tags.
    """
    dao = _update_tags_dao(mock_tag_dao_cls)
    new_tags = [tag for tag in MOCK_TAGS if tag.type == TagType.custom]
    tag_to_be_removed = new_tags.pop()
    new_tag = Tag(id=10, name="my new tag", type=TagType.custom)
    new_tags.append(new_tag)
    new_tag_ids = [tag.id for tag in new_tags]

    dao.find_by_ids = AsyncMock(return_value=[new_tag])

    await update_tags(object_type, 1, MOCK_TAGS, new_tag_ids, MagicMock())

    dao.delete_tagged_object.assert_called_once_with(
        object_type.name, 1, tag_to_be_removed.name
    )
    dao.find_by_ids.assert_called_once_with([new_tag.id])
    dao.create_custom_tagged_objects.assert_called_once_with(
        object_type.name, 1, ["my new tag"]
    )


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
@patch("superset.db.daos.tag.AsyncTagDAO")
async def test_update_tags_removing_all_tags(mock_tag_dao_cls, object_type):
    """
    Test the ``update_tags`` method when removing all tags.
    """
    dao = _update_tags_dao(mock_tag_dao_cls)
    await update_tags(object_type, 1, MOCK_TAGS, [], MagicMock())

    dao.delete_tagged_object.assert_has_calls(
        [
            call(object_type.name, 1, tag.name)
            for tag in MOCK_TAGS
            if tag.type == TagType.custom
        ]
    )
    dao.create_custom_tagged_objects.assert_not_called()


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
@patch("superset.db.daos.tag.AsyncTagDAO")
async def test_update_tags_no_tags(mock_tag_dao_cls, object_type):
    """
    Test the ``update_tags`` method when the asset only has system tags.
    """
    dao = _update_tags_dao(mock_tag_dao_cls)
    system_tags = [tag for tag in MOCK_TAGS if tag.type != TagType.custom]
    new_tags = [tag for tag in MOCK_TAGS if tag.type == TagType.custom]
    new_tag_ids = [tag.id for tag in new_tags]
    new_tag_names = [tag.name for tag in new_tags]

    dao.find_by_ids = AsyncMock(return_value=new_tags)

    await update_tags(object_type, 1, system_tags, new_tag_ids, MagicMock())

    dao.delete_tagged_object.assert_not_called()
    dao.find_by_ids.assert_called_once_with(new_tag_ids)
    dao.create_custom_tagged_objects.assert_called_once_with(
        object_type.name, 1, new_tag_names
    )
