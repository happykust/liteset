"""Backward-compatible 'superset' CLI that delegates to liteset."""
from __future__ import annotations

import warnings

import click

from liteset.cli.main import liteset_cli, normalize_token


@click.group(context_settings={"token_normalize_func": normalize_token})
@click.pass_context
def superset_cli(ctx: click.Context) -> None:
    """Legacy Superset CLI (deprecated — use 'liteset' instead)."""
    warnings.warn(
        "The 'superset' command is deprecated. Use 'liteset' instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    ctx.ensure_object(dict)


for cmd_name, cmd in liteset_cli.commands.items():
    superset_cli.add_command(cmd, cmd_name)
