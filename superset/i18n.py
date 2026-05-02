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
"""Contextvars-based i18n.

Provides gettext() and lazy_gettext() compatible with Superset's
translation patterns but without any Flask dependency.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from typing import Any

_current_locale: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_locale", default="en"
)

# locale -> {msgid -> translated}
_translations: dict[str, dict[str, str]] = {}


def init_translations(translations: dict[str, dict[str, str]]) -> None:
    """Load translation catalogs at startup."""
    _translations.update(translations)


def set_locale(locale: str) -> contextvars.Token[str]:
    """Set locale and return token for reset."""
    return _current_locale.set(locale)


def get_locale() -> str:
    return _current_locale.get()


def gettext(msgid: str, **variables: Any) -> str:
    """Translate a string using the current locale.

    When ``variables`` are supplied the translated string is interpolated
    via ``result % variables`` so that ``%(name)s``-style placeholders are
    filled in (mirrors the legacy gettext API surface).
    """
    locale = _current_locale.get()
    catalog = _translations.get(locale, {})
    result = catalog.get(msgid, msgid)
    if variables:
        return result % variables
    return result


class LazyString:
    """Proxy that defers gettext resolution until string is used."""

    __slots__ = ("_msgid", "_variables")

    def __init__(self, msgid: str, **variables: Any) -> None:
        self._msgid = msgid
        self._variables: dict[str, Any] = variables

    def _resolve(self) -> str:
        return gettext(self._msgid, **self._variables)

    def __str__(self) -> str:
        return self._resolve()

    def __repr__(self) -> str:
        return f"LazyString({self._msgid!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, LazyString):
            return str(self) == str(other)
        return str(self) == other

    def __hash__(self) -> int:
        return hash(str(self))

    def __add__(self, other: Any) -> str:
        return str(self) + str(other)

    def __radd__(self, other: Any) -> str:
        return str(other) + str(self)

    def __mod__(self, other: Any) -> str:
        return str(self) % other

    def __bool__(self) -> bool:
        return bool(str(self))

    def __len__(self) -> int:
        return len(str(self))

    def __contains__(self, item: Any) -> bool:
        return item in str(self)

    def __iter__(self) -> Iterator[str]:
        return iter(str(self))

    def __getitem__(self, key: int | slice) -> str:
        return str(self)[key]

    def __format__(self, format_spec: str) -> str:
        return format(str(self), format_spec)


def lazy_gettext(msgid: str, **variables: Any) -> LazyString:
    """Return a lazy proxy that resolves translation on access.

    ``variables`` are stored on the proxy and applied via ``%`` interpolation
    once the string is materialised (matches the legacy lazy-gettext API).
    """
    return LazyString(msgid, **variables)


# ``_`` is the conventional alias used across the codebase for the active
# gettext callable.
_ = gettext
