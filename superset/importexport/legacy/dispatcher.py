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
"""Version-tolerant dispatchers for dashboard / dataset imports.

Direct port of:

* ``superset_old/commands/dashboard/importers/dispatcher.py``
* ``superset_old/commands/dataset/importers/dispatcher.py``

The dispatchers iterate over the registered command versions (v1 first,
v0 last because v0 files are not versioned) and run the first one that
does NOT raise :class:`IncorrectVersionError` against the supplied
contents.  A :class:`CommandInvalidError` from a matched version
short-circuits the search — that's the original behaviour and ensures
real validation errors aren't masked by trying older formats.

Inputs:

* ``contents`` — the canonical ``{filename: text}`` mapping consumed by
  v0 commands directly.  v1 commands accept :class:`io.BytesIO`
  containing a ZIP, so the dispatcher converts the dict to a fresh ZIP
  archive before invoking v1.  The original Flask-side ``ImportAssetsCommand``
  wired the v1 path the same way (parse the ZIP into a dict on entry,
  then ``v1`` rebuilds whatever it needs from the dict directly via
  :func:`load_yaml`); for v1 dashboard / dataset CLI paths the original
  command accepts a ``dict[str, str]`` already, so no conversion is
  needed there either.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from superset.commands.importers.exceptions import IncorrectVersionError
from superset.exceptions import CommandException, CommandInvalidError
from superset.importexport.legacy.dashboard_v0 import (
    ImportDashboardsCommand as V0ImportDashboardsCommand,
)
from superset.importexport.legacy.dataset_v0 import (
    ImportDatasetsCommand as V0ImportDatasetsCommand,
)

logger = logging.getLogger(__name__)


class ImportDashboardsCommand:
    """Try every registered dashboard import version in order.

    The original command order is preserved: v1 is attempted first; v0
    is the final fallback because legacy JSON dumps carry no version
    metadata.
    """

    def __init__(
        self,
        contents: dict[str, str],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.contents = contents
        self.args = args
        self.kwargs = kwargs

    def run(self, session: Any | None = None) -> None:
        """Run the first version that matches the supplied contents."""
        # ``v1`` is the modern path — since v1 expects ``contents`` as a
        # dict in its CLI form (matching the original v1 dataset/dashboard
        # commands), we first try the high-level dispatcher path and only
        # fall through to v0 on :class:`IncorrectVersionError`.
        try:
            from superset.commands.dashboard.importers.v1 import (  # noqa: PLC0415
                ImportDashboardsCommand as V1ImportDashboardsCommand,
            )
        except Exception:  # noqa: BLE001
            V1ImportDashboardsCommand = None  # type: ignore[assignment,misc]  # noqa: N806

        if V1ImportDashboardsCommand is not None:
            try:
                cmd = V1ImportDashboardsCommand(
                    self.contents, *self.args, **self.kwargs
                )
                # v1 commands are async; only invoke when the caller is
                # already running inside an event loop (via :func:`asyncio.run`
                # for CLI users).  Otherwise skip straight to v0.
                run_method = getattr(cmd, "run", None)
                if run_method is not None and not _is_coroutine_method(run_method):
                    run_method()
                    return
                # Async run() — let the caller handle the coroutine.
                # This dispatcher only fully implements the sync v0 path;
                # v1 is the responsibility of the controller layer where
                # an event loop is already available.
                logger.debug("v1 import is async; skipping in sync dispatcher")
            except IncorrectVersionError:
                logger.debug("File not handled by v1, trying v0")
            except (CommandInvalidError, CommandException):
                logger.info("v1 command failed validation")
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Error running v1 import command")
                raise

        # v0 fallback — sync, dict-based.
        try:
            v0_cmd = V0ImportDashboardsCommand(
                self.contents, *self.args, **self.kwargs
            )
            v0_cmd.run(session=session)
            return
        except IncorrectVersionError:
            logger.debug("File not handled by v0 either")
        except (CommandInvalidError, CommandException):
            logger.info("v0 command failed validation")
            raise

        raise CommandInvalidError("Could not find a valid command to import file")

    async def run_async(
        self,
        *,
        dao: Any | None = None,
        session: Any | None = None,
    ) -> None:
        """Async port of the dispatcher's version-tolerant ``run``.

        Wires the v0 fallback into the HTTP import path: tries the async v1
        command first (building a ZIP from the ``{filename: text}`` contents
        the controller parsed) and, on :class:`IncorrectVersionError`, falls
        back to the sync v0 command (run in a thread).  This mirrors the
        original ``ImportDashboardsCommand`` dispatcher
        (``superset_old/commands/dashboard/importers/dispatcher.py``) which
        the Flask API called directly.
        """
        try:
            from superset.commands.dashboard.importers.v1 import (  # noqa: PLC0415
                ImportDashboardsCommand as V1ImportDashboardsCommand,  # noqa: N814
            )
        except Exception:  # noqa: BLE001
            v1_command = None
        else:
            v1_command = V1ImportDashboardsCommand

        if v1_command is not None:
            try:
                cmd = v1_command(
                    _contents_to_zip(self.contents),
                    *self.args,
                    dao=dao,
                    **self.kwargs,
                )
                await cmd.execute()
                return
            except IncorrectVersionError:
                logger.debug("File not handled by v1, trying v0")
            except (CommandInvalidError, CommandException):
                logger.info("v1 command failed validation")
                raise

        await _run_v0_async(
            V0ImportDashboardsCommand,
            self.contents,
            self.args,
            self.kwargs,
            session,
        )

    def validate(self) -> None:
        """No-op — each underlying version validates its own contents."""


class ImportDatasetsCommand:
    """Try every registered dataset import version in order."""

    def __init__(
        self,
        contents: dict[str, str],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.contents = contents
        self.args = args
        self.kwargs = kwargs

    def run(self, session: Any | None = None) -> None:
        try:
            from superset.commands.dataset.importers.v1 import (  # noqa: PLC0415
                ImportDatasetsCommand as V1ImportDatasetsCommand,
            )
        except Exception:  # noqa: BLE001
            V1ImportDatasetsCommand = None  # type: ignore[assignment,misc]  # noqa: N806

        if V1ImportDatasetsCommand is not None:
            try:
                cmd = V1ImportDatasetsCommand(
                    self.contents, *self.args, **self.kwargs
                )
                run_method = getattr(cmd, "run", None)
                if run_method is not None and not _is_coroutine_method(run_method):
                    run_method()
                    return
                logger.debug("v1 import is async; skipping in sync dispatcher")
            except IncorrectVersionError:
                logger.debug("File not handled by v1, trying v0")
            except (CommandInvalidError, CommandException):
                logger.info("v1 command failed validation")
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Error running v1 import command")
                raise

        try:
            v0_cmd = V0ImportDatasetsCommand(self.contents, *self.args, **self.kwargs)
            v0_cmd.run(session=session)
            return
        except IncorrectVersionError:
            logger.debug("File not handled by v0 either")
        except (CommandInvalidError, CommandException):
            logger.info("v0 command failed validation")
            raise

        raise CommandInvalidError("Could not find a valid command to import file")

    async def run_async(
        self,
        *,
        dao: Any | None = None,
        session: Any | None = None,
    ) -> None:
        """Async port of the dispatcher — async v1 first, sync v0 fallback.

        See :meth:`ImportDashboardsCommand.run_async`.
        """
        try:
            from superset.commands.dataset.importers.v1 import (  # noqa: PLC0415
                ImportDatasetsCommand as V1ImportDatasetsCommand,  # noqa: N814
            )
        except Exception:  # noqa: BLE001
            v1_command = None
        else:
            v1_command = V1ImportDatasetsCommand

        if v1_command is not None:
            try:
                cmd = v1_command(
                    _contents_to_zip(self.contents),
                    *self.args,
                    dao=dao,
                    **self.kwargs,
                )
                await cmd.execute()
                return
            except IncorrectVersionError:
                logger.debug("File not handled by v1, trying v0")
            except (CommandInvalidError, CommandException):
                logger.info("v1 command failed validation")
                raise

        await _run_v0_async(
            V0ImportDatasetsCommand,
            self.contents,
            self.args,
            self.kwargs,
            session,
        )

    def validate(self) -> None:
        """No-op — each underlying version validates its own contents."""


def _is_coroutine_method(func: Any) -> bool:
    """Return ``True`` when ``func`` is an async method/coroutine function."""
    import asyncio
    import inspect

    if asyncio.iscoroutinefunction(func):
        return True
    if inspect.iscoroutinefunction(getattr(func, "__func__", func)):
        return True
    return False


def _contents_to_zip(contents: dict[str, str]) -> io.BytesIO:
    """Pack a ``{filename: text}`` mapping into an in-memory ZIP for v1.

    The async v1 import commands consume an :class:`io.BytesIO` ZIP, whereas
    the dispatcher (mirroring the original) carries the bundle as a dict.  The
    v1 command's ``_parse_zip`` re-applies ``remove_root``/``is_valid_config``,
    so a flat ``{name: text}`` round-trips unchanged.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in contents.items():
            zf.writestr(name, text)
    buf.seek(0)
    return buf


async def _run_v0_async(
    v0_cls: Any,
    contents: dict[str, str],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    session: Any | None,
) -> None:
    """Run a sync v0 command in a worker thread (it uses a sync Session)."""
    import asyncio

    def _run() -> None:
        try:
            v0_cls(contents, *args, **kwargs).run(session=session)
            return
        except IncorrectVersionError:
            logger.debug("File not handled by v0 either")
        except (CommandInvalidError, CommandException):
            logger.info("v0 command failed validation")
            raise
        raise CommandInvalidError("Could not find a valid command to import file")

    await asyncio.to_thread(_run)


__all__ = ["ImportDashboardsCommand", "ImportDatasetsCommand"]
