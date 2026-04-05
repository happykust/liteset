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
"""User management CLI commands.

Provides ``create-admin``, ``create-user``, ``list-users``,
``reset-password``, and ``load-test-users``.  Also exposes the
``fab`` sub-group for backward-compatible ``superset fab create-admin``
style invocations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import click

from superset.utils.password import generate_password_hash as _hash_password

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _get_async_session_factory() -> tuple[Any, Any]:
    """Create an async session factory from current settings."""
    from superset.config import SupersetSettings
    from superset.db.engine import (
        create_db_engine,
        create_session_factory,
    )

    settings = SupersetSettings()  # type: ignore[call-arg]
    engine = create_db_engine(
        settings.sqlalchemy_database_uri,
    )
    return create_session_factory(engine), engine


# ------------------------------------------------------------------
# create-admin
# ------------------------------------------------------------------


@click.command("create-admin")
@click.option("--username", default="admin", prompt="Username")
@click.option("--firstname", default="admin", prompt="User first name")
@click.option("--lastname", default="user", prompt="User last name")
@click.option("--email", default="admin@fab.org", prompt="Email")
@click.password_option()
def create_admin(
    username: str,
    firstname: str,
    lastname: str,
    email: str,
    password: str,
) -> None:
    """Create an admin user."""
    import anyio

    async def _create() -> None:
        from sqlalchemy import text

        session_factory, engine = _get_async_session_factory()
        async with session_factory() as session:
            # Check if user already exists
            result = await session.execute(
                text("SELECT id FROM ab_user WHERE username = :u"),
                {"u": username},
            )
            if result.first() is not None:
                click.secho(f"Error! User already exists: {username}", fg="red")
                await engine.dispose()
                return

            result = await session.execute(
                text("SELECT id FROM ab_user WHERE email = :e"),
                {"e": email},
            )
            if result.first() is not None:
                click.secho(f"Error! Email already in use: {email}", fg="red")
                await engine.dispose()
                return

            # Find or create Admin role
            result = await session.execute(
                text("SELECT id FROM ab_role WHERE name = 'Admin'"),
            )
            role_row = result.first()
            if role_row is None:
                click.secho(
                    "Error! Admin role not found. Run 'superset init' first.",
                    fg="red",
                )
                await engine.dispose()
                return
            role_id = role_row[0]

            # Insert user
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            hashed = _hash_password(password)
            await session.execute(
                text(
                    "INSERT INTO ab_user "
                    "(first_name, last_name, username, email, password, "
                    " active, created_on, changed_on) "
                    "VALUES (:fn, :ln, :un, :em, :pw, :act, :co, :ch)"
                ),
                {
                    "fn": firstname,
                    "ln": lastname,
                    "un": username,
                    "em": email,
                    "pw": hashed,
                    "act": True,
                    "co": now,
                    "ch": now,
                },
            )

            # Get the new user's id
            result = await session.execute(
                text("SELECT id FROM ab_user WHERE username = :u"),
                {"u": username},
            )
            user_id = result.scalar_one()

            # Associate user with Admin role
            await session.execute(
                text("INSERT INTO ab_user_role (user_id, role_id) VALUES (:uid, :rid)"),
                {"uid": user_id, "rid": role_id},
            )
            await session.commit()
            click.secho(f"Admin user {username} created.", fg="green")

        await engine.dispose()

    anyio.run(_create)


# ------------------------------------------------------------------
# create-user
# ------------------------------------------------------------------


@click.command("create-user")
@click.option("--role", default="Public", prompt="Role")
@click.option("--username", prompt="Username")
@click.option("--firstname", prompt="User first name")
@click.option("--lastname", prompt="User last name")
@click.option("--email", prompt="Email")
@click.password_option()
def create_user(
    role: str,
    username: str,
    firstname: str,
    lastname: str,
    email: str,
    password: str,
) -> None:
    """Create a user with a given role."""
    import anyio

    async def _create() -> None:
        from sqlalchemy import text

        session_factory, engine = _get_async_session_factory()
        async with session_factory() as session:
            # Check duplicates
            result = await session.execute(
                text("SELECT id FROM ab_user WHERE username = :u"),
                {"u": username},
            )
            if result.first() is not None:
                click.secho(f"Error! User already exists: {username}", fg="red")
                await engine.dispose()
                return

            result = await session.execute(
                text("SELECT id FROM ab_user WHERE email = :e"),
                {"e": email},
            )
            if result.first() is not None:
                click.secho(f"Error! Email already in use: {email}", fg="red")
                await engine.dispose()
                return

            # Find role
            result = await session.execute(
                text("SELECT id FROM ab_role WHERE name = :r"),
                {"r": role},
            )
            role_row = result.first()
            if role_row is None:
                click.secho(f"Error! Role not found: {role}", fg="red")
                await engine.dispose()
                return
            role_id = role_row[0]

            # Insert user
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            hashed = _hash_password(password)
            await session.execute(
                text(
                    "INSERT INTO ab_user "
                    "(first_name, last_name, username, email, password, "
                    " active, created_on, changed_on) "
                    "VALUES (:fn, :ln, :un, :em, :pw, :act, :co, :ch)"
                ),
                {
                    "fn": firstname,
                    "ln": lastname,
                    "un": username,
                    "em": email,
                    "pw": hashed,
                    "act": True,
                    "co": now,
                    "ch": now,
                },
            )

            result = await session.execute(
                text("SELECT id FROM ab_user WHERE username = :u"),
                {"u": username},
            )
            user_id = result.scalar_one()

            await session.execute(
                text("INSERT INTO ab_user_role (user_id, role_id) VALUES (:uid, :rid)"),
                {"uid": user_id, "rid": role_id},
            )
            await session.commit()
            click.secho(f"User {username} created.", fg="green")

        await engine.dispose()

    anyio.run(_create)


# ------------------------------------------------------------------
# reset-password
# ------------------------------------------------------------------


@click.command("reset-password")
@click.option(
    "--username",
    default="admin",
    prompt="Username",
    help="Username whose password to reset.",
)
@click.password_option()
def reset_password(username: str, password: str) -> None:
    """Reset a user's password."""
    import anyio

    async def _reset() -> None:
        from sqlalchemy import text

        session_factory, engine = _get_async_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT id FROM ab_user WHERE username = :u"),
                {"u": username},
            )
            row = result.first()
            if row is None:
                click.secho(f"User {username} not found.", fg="red")
                await engine.dispose()
                return

            hashed = _hash_password(password)
            await session.execute(
                text("UPDATE ab_user SET password = :pw WHERE username = :u"),
                {"pw": hashed, "u": username},
            )
            await session.commit()
            click.secho(f"Password for user {username} reset.", fg="green")

        await engine.dispose()

    anyio.run(_reset)


# ------------------------------------------------------------------
# list-users
# ------------------------------------------------------------------


@click.command("list-users")
def list_users() -> None:
    """List all users."""
    import anyio

    async def _list() -> None:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.security import User

        session_factory, engine = _get_async_session_factory()
        async with session_factory() as session:
            stmt = select(User).options(selectinload(User.roles)).order_by(User.id)
            result = await session.execute(stmt)
            users = result.scalars().all()
            if not users:
                click.echo("No users found.")
                await engine.dispose()
                return

            click.echo(f"{'ID':>4}  {'Username':<20} {'Email':<30} {'Active':<7} Roles")
            click.echo("-" * 80)
            for u in users:
                active_str = "Yes" if u.active else "No"
                roles = ", ".join(r.name for r in u.roles)
                click.echo(
                    f"{u.id:>4}  {u.username:<20} {u.email:<30} {active_str:<7} {roles}"
                )

        await engine.dispose()

    anyio.run(_list)


# ------------------------------------------------------------------
# load-test-users (development helper)
# ------------------------------------------------------------------


_TEST_USERS = [
    ("admin", "Admin", "User", "admin@superset.org", "Admin"),
    ("alpha", "Alpha", "User", "alpha@superset.org", "Alpha"),
    ("gamma", "Gamma", "User", "gamma@superset.org", "Gamma"),
    ("gamma2", "Gamma2", "User", "gamma2@superset.org", "Gamma"),
    ("gamma_sqllab", "Gamma Sqllab", "User", "gamma_sqllab@superset.org", "sql_lab"),
]


@click.command("load-test-users")
@click.option(
    "--password",
    default="general",
    help="Password for all test users (default: general)",
)
def load_test_users(password: str) -> None:
    """Create standard test users for development."""
    import anyio

    async def _load() -> None:
        from sqlalchemy import text

        session_factory, engine = _get_async_session_factory()
        async with session_factory() as session:
            hashed = _hash_password(password)
            now = datetime.now(timezone.utc).replace(tzinfo=None)

            for uname, fname, lname, email, role_name in _TEST_USERS:
                # Skip if already exists
                result = await session.execute(
                    text("SELECT id FROM ab_user WHERE username = :u"),
                    {"u": uname},
                )
                if result.first() is not None:
                    click.echo(f"  User {uname} already exists, skipping.")
                    continue

                # Find role
                result = await session.execute(
                    text("SELECT id FROM ab_role WHERE name = :r"),
                    {"r": role_name},
                )
                role_row = result.first()
                if role_row is None:
                    click.secho(
                        f"  Role {role_name} not found for user {uname}, skipping.",
                        fg="yellow",
                    )
                    continue
                role_id = role_row[0]

                await session.execute(
                    text(
                        "INSERT INTO ab_user "
                        "(first_name, last_name, username, email, password, "
                        " active, created_on, changed_on) "
                        "VALUES (:fn, :ln, :un, :em, :pw, :act, :co, :ch)"
                    ),
                    {
                        "fn": fname,
                        "ln": lname,
                        "un": uname,
                        "em": email,
                        "pw": hashed,
                        "act": True,
                        "co": now,
                        "ch": now,
                    },
                )
                result = await session.execute(
                    text("SELECT id FROM ab_user WHERE username = :u"),
                    {"u": uname},
                )
                user_id = result.scalar_one()
                await session.execute(
                    text(
                        "INSERT INTO ab_user_role (user_id, role_id) "
                        "VALUES (:uid, :rid)"
                    ),
                    {"uid": user_id, "rid": role_id},
                )
                click.secho(f"  Created test user: {uname} ({role_name})", fg="green")

            await session.commit()
        await engine.dispose()
        click.echo("Test users loaded.")

    anyio.run(_load)


# ------------------------------------------------------------------
# fab sub-group (backward-compatible aliases)
# ------------------------------------------------------------------


@click.group("fab")
def fab_group() -> None:
    """FAB-compatible commands (backward compatibility)."""


# Register aliases in the fab sub-group
fab_group.add_command(create_admin, "create-admin")
fab_group.add_command(create_user, "create-user")
fab_group.add_command(list_users, "list-users")
fab_group.add_command(reset_password, "reset-password")
