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
"""Renderers for Marshmallow validator/function ``__repr__`` strings.

Apache Superset's ``GET /<resource>/_info`` payload echoes the
``str(...)`` representation of every Marshmallow validator attached to
each schema field.  Examples observed in the snapshots::

    "<Length(min=None, max=250, equal=None, error=None)>"
    "<Range(min=1, max=None, min_inclusive=True, max_inclusive=True,"
    " error='Value must be greater than 0')>"
    "<OneOf(choices=('Alert', 'Report'), labels=[],"
    " error='Must be one of: {choices}.')>"
    "<function validate_json at 0xMEM>"

These strings are reproduced here verbatim so the dynamic ``_info``
builder can match the contract snapshots byte-for-byte without
depending on Marshmallow itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def length(
    min_: int | None = None,
    max_: int | None = None,
    equal: int | None = None,
    error: str | None = None,
) -> str:
    """Render Marshmallow ``Length`` validator ``__repr__``."""
    err_repr = repr(error) if error is not None else None
    return f"<Length(min={min_}, max={max_}, equal={equal}, error={err_repr})>"


def range_(
    min_: int | float | None = None,
    max_: int | float | None = None,
    min_inclusive: bool = True,
    max_inclusive: bool = True,
    error: str | None = None,
) -> str:
    """Render Marshmallow ``Range`` validator ``__repr__``."""
    err = repr(error) if error is not None else None
    return (
        f"<Range(min={min_}, max={max_}, "
        f"min_inclusive={min_inclusive}, max_inclusive={max_inclusive}, "
        f"error={err})>"
    )


def one_of(
    choices: Sequence[str],
    labels: Iterable[str] = (),
    error: str = "Must be one of: {choices}.",
) -> str:
    """Render Marshmallow ``OneOf`` validator ``__repr__``."""
    choices_repr = (
        "(" + ", ".join(repr(c) for c in choices) + (",)" if len(choices) == 1 else ")")
    )
    labels_repr = "[" + ", ".join(repr(label) for label in labels) + "]"
    return f"<OneOf(choices={choices_repr}, labels={labels_repr}, error={error!r})>"


def function(name: str) -> str:
    """Render the ``__repr__`` of a free function used as a validator.

    The actual hex memory address is replaced with the literal token
    ``0xMEM`` to match how snapshots normalise dynamic addresses.
    """
    return f"<function {name} at 0xMEM>"


__all__ = ["length", "range_", "one_of", "function"]
