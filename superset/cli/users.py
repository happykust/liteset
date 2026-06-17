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
            click.secho(f"Admin user {username} created.", fg="green")

        await engine.dispose()

    anyio.run(_create)


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


_TEST_USERS = [
    # Must match original FAB test users exactly: first_name=username,
    # last_name="user", email=username@fab.org — Cypress tests assert on
    # the rendered "alpha user" string and email addresses.
    ("admin", "admin", "user", "admin@fab.org", "Admin"),
    ("alpha", "alpha", "user", "alpha@fab.org", "Alpha"),
    ("gamma", "gamma", "user", "gamma@fab.org", "Gamma"),
    ("gamma2", "gamma2", "user", "gamma2@fab.org", "Gamma"),
    ("gamma_sqllab", "gamma_sqllab", "user", "gamma_sqllab@fab.org", "gamma_sqllab"),
    ("gamma_no_csv", "gamma_no_csv", "user", "gamma_no_csv@fab.org", "gamma_no_csv"),
]


@click.command("load-test-users")
@click.option(
    "--password",
    default="general",
    help="Password for all test users (default: general)",
)
def load_test_users(password: str) -> None:  # noqa: C901  # complex business logic
    """Create standard test users for development."""
    import anyio

    async def _load() -> None:  # noqa: C901  # complex business logic
        from superset.config import SupersetSettings

        if not SupersetSettings().testing:  # type: ignore[call-arg]
            click.secho(
                "load-test-users only runs when TESTING is enabled.",
                fg="yellow",
            )
            return

        session_factory, engine = _get_async_session_factory()
        async with session_factory() as session:
            from superset.models.security import (
                PermissionView,
                Role,
                User,
            )
            from superset.security.dao import AsyncSecurityDAO

            dao = AsyncSecurityDAO(session)
            hashed = _hash_password(password)
            now = datetime.now(timezone.utc).replace(tzinfo=None)

            # -----------------------------------------------------------
            # Sync standard FAB roles (Admin, Alpha, Gamma, Public,
            # sql_lab) BEFORE creating users.  Without this, the
            # subsequent user creation loop cannot find the "Admin" /
            # "Alpha" / "Gamma" / "sql_lab" roles (they are created by
            # ``superset init``, which runs AFTER ``load_test_users`` in
            # the Cypress bootstrap flow in docker/docker-init.sh).
            # -----------------------------------------------------------
            from superset.config import SupersetSettings
            from superset.security.sync_roles import sync_role_definitions

            settings = SupersetSettings()  # type: ignore[call-arg]
            public_role_like = getattr(settings, "public_role_like", None)
            sync_summary = await sync_role_definitions(
                session,
                public_role_like=public_role_like,
            )
            click.secho(
                f"  Synced standard roles: {sync_summary}",
                fg="green",
            )
            await session.flush()

            # -----------------------------------------------------------
            # Resolve the example DB's ``database_access`` PVM so the custom
            # test roles can run SQL Lab against the examples database
            # (examples_pv granted to BOTH gamma_sqllab and gamma_no_csv).
            # Uses THIS async session rather than a separate sync session to
            # avoid a cross-session identity mismatch when appending the PVM to
            # the roles below.
            # -----------------------------------------------------------
            from sqlalchemy import select as _select

            from superset.constants import EXAMPLES_DB_UUID
            from superset.models.core import Database
            from superset.security.sync_roles import _get_or_create_pvm

            examples_res = await session.execute(
                _select(Database).where(Database.database_name == "examples")
            )
            examples_db = examples_res.scalars().one_or_none()
            if examples_db is None:
                examples_uri = (
                    getattr(settings, "sqlalchemy_examples_uri", None)
                    or settings.sqlalchemy_database_uri
                )
                examples_db = Database(database_name="examples", uuid=EXAMPLES_DB_UUID)
                session.add(examples_db)
                examples_db.set_sqlalchemy_uri(str(examples_uri))
                await session.flush()  # assign id so ``.perm`` resolves
            examples_pv = await _get_or_create_pvm(
                session, "database_access", examples_db.perm
            )

            # -----------------------------------------------------------
            # Create custom test roles (gamma_sqllab, gamma_no_csv).
            #
            # gamma_sqllab = Gamma + sql_lab permissions
            # gamma_no_csv = Gamma + sql_lab permissions MINUS "can csv on Superset"
            # -----------------------------------------------------------
            for custom_role_name in ("gamma_sqllab", "gamma_no_csv"):
                existing = await dao.get_role_by_name(custom_role_name)
                if existing is not None:
                    continue

                new_role = Role(name=custom_role_name)
                session.add(new_role)
                await session.flush()
                await session.refresh(new_role, ["permissions"])

                # Collect permissions from Gamma + sql_lab source roles
                source_pvs: list[PermissionView] = []
                for source_name in ("Gamma", "sql_lab"):
                    source_role = await dao.get_role_by_name(source_name)
                    if source_role is None:
                        continue
                    # Eagerly load permissions for this role
                    await session.refresh(source_role, ["permissions"])
                    source_pvs.extend(source_role.permissions or [])

                # Deduplicate
                seen_pv_ids: set[int] = set()
                for pv in source_pvs:
                    if pv.id in seen_pv_ids:
                        continue
                    seen_pv_ids.add(int(pv.id))

                    # For gamma_no_csv, skip "can csv on Superset"
                    if custom_role_name == "gamma_no_csv":
                        await session.refresh(pv, ["permission", "view_menu"])
                        p_name = getattr(pv.permission, "name", "")
                        vm_name = getattr(pv.view_menu, "name", "")
                        if p_name == "can_csv" and vm_name == "Superset":
                            continue

                    new_role.permissions.append(pv)

                # Grant example-DB ``database_access`` to BOTH custom roles —
                # upstream cli/test.py:53-55 adds examples_pv to gamma_sqllab
                # AND gamma_no_csv (the standard Gamma/sql_lab role defs do not
                # include it, so copying their perms above is not enough).
                if examples_pv.id not in {p.id for p in new_role.permissions}:
                    new_role.permissions.append(examples_pv)

                await session.flush()
                click.secho(
                    f"  Created role: {custom_role_name} "
                    f"({len(new_role.permissions)} permissions)",
                    fg="green",
                )

            for uname, fname, lname, email, role_name in _TEST_USERS:
                existing_user = await dao.get_user_by_username(uname)
                if existing_user is not None:
                    click.echo(f"  User {uname} already exists, skipping.")
                    continue

                role = await dao.get_role_by_name(role_name)
                if role is None:
                    click.secho(
                        f"  Role {role_name} not found for user {uname}, skipping.",
                        fg="yellow",
                    )
                    continue

                user = User(
                    first_name=fname,
                    last_name=lname,
                    username=uname,
                    email=email,
                    password=hashed,
                    active=True,
                    created_on=now,
                    changed_on=now,
                )
                user.roles = [role]
                session.add(user)
                click.secho(f"  Created test user: {uname} ({role_name})", fg="green")

            await session.commit()
        await engine.dispose()
        click.echo("Test users loaded.")

    anyio.run(_load)


@click.group("fab")
def fab_group() -> None:
    """FAB-compatible commands (backward compatibility)."""


fab_group.add_command(create_admin, "create-admin")
fab_group.add_command(create_user, "create-user")
fab_group.add_command(list_users, "list-users")
fab_group.add_command(reset_password, "reset-password")
