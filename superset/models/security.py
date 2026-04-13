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
"""FAB security table models (pure SQLAlchemy).

These mirror Flask-AppBuilder's ab_user, ab_role, ab_permission,
ab_view_menu, ab_permission_view tables so superset can query them
without importing flask_appbuilder.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Table
from sqlalchemy.orm import relationship

from superset.models.helpers import Base, metadata

# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------

ab_user_role = Table(
    "ab_user_role",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("ab_user.id")),
    Column("role_id", Integer, ForeignKey("ab_role.id")),
)

ab_permission_view_role = Table(
    "ab_permission_view_role",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("permission_view_id", Integer, ForeignKey("ab_permission_view.id")),
    Column("role_id", Integer, ForeignKey("ab_role.id")),
)

ab_user_group = Table(
    "ab_user_group",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("ab_user.id")),
    Column("group_id", Integer, ForeignKey("ab_group.id")),
)

ab_group_role = Table(
    "ab_group_role",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("group_id", Integer, ForeignKey("ab_group.id")),
    Column("role_id", Integer, ForeignKey("ab_role.id")),
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class User(Base):
    """Maps to Flask-AppBuilder's ``ab_user`` table."""

    __tablename__ = "ab_user"

    id = Column(Integer, primary_key=True)
    first_name = Column(String(256), nullable=False)
    last_name = Column(String(256), nullable=False)
    username = Column(String(512), unique=True, nullable=False)
    password = Column(String(256))
    active = Column(Boolean, default=True)
    email = Column(String(512), unique=True, nullable=False)
    last_login = Column(DateTime, nullable=True)
    login_count = Column(Integer, default=0)
    fail_login_count = Column(Integer, default=0)
    created_on = Column(DateTime, nullable=True)
    changed_on = Column(DateTime, nullable=True)

    roles = relationship("Role", secondary=ab_user_role, backref="user")
    groups = relationship("Group", secondary=ab_user_group, backref="users")

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def __repr__(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Role(Base):
    """Maps to Flask-AppBuilder's ``ab_role`` table."""

    __tablename__ = "ab_role"

    id = Column(Integer, primary_key=True)
    name = Column(String(256), unique=True, nullable=False)

    permissions = relationship(
        "PermissionView",
        secondary=ab_permission_view_role,
        backref="role",
    )
    groups = relationship("Group", secondary=ab_group_role, backref="roles_")


class Group(Base):
    """Maps to Flask-AppBuilder's ``ab_group`` table."""

    __tablename__ = "ab_group"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    label = Column(String(150), nullable=True)
    description = Column(String(512), nullable=True)


class Permission(Base):
    """Maps to Flask-AppBuilder's ``ab_permission`` table."""

    __tablename__ = "ab_permission"

    id = Column(Integer, primary_key=True)
    name = Column(String(512), unique=True, nullable=False)


class ViewMenu(Base):
    """Maps to Flask-AppBuilder's ``ab_view_menu`` table."""

    __tablename__ = "ab_view_menu"

    id = Column(Integer, primary_key=True)
    name = Column(String(512), unique=True, nullable=False)


class PermissionView(Base):
    """Maps to Flask-AppBuilder's ``ab_permission_view`` table."""

    __tablename__ = "ab_permission_view"

    id = Column(Integer, primary_key=True)
    permission_id = Column(Integer, ForeignKey("ab_permission.id"))
    view_menu_id = Column(Integer, ForeignKey("ab_view_menu.id"))

    permission = relationship("Permission")
    view_menu = relationship("ViewMenu")


class RegisterUser(Base):
    """Maps to Flask-AppBuilder's ``ab_register_user`` table.

    Stores pending user registration requests that are awaiting
    email activation. Once activated, the user is created in
    ``ab_user`` and the registration row is deleted.
    """

    __tablename__ = "ab_register_user"

    id = Column(Integer, primary_key=True)
    first_name = Column(String(64), nullable=False)
    last_name = Column(String(64), nullable=False)
    username = Column(String(128), unique=True, nullable=False)
    password = Column(String(256), nullable=True)
    email = Column(String(320), unique=True, nullable=False)
    registration_date = Column(DateTime, nullable=True)
    registration_hash = Column(String(256), nullable=True)

    def __str__(self) -> str:
        return f"{self.username}"

    def __repr__(self) -> str:
        return f"<RegisterUser {self.username}>"
