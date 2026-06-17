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
"""Migration shared utilities needed by the runtime ``migrate-viz`` CLI command.

This module exposes two functions the migrate-viz pipeline depends on
(``paginated_update`` and ``try_load_json``) plus :func:`has_table`
which the chart-migration processors call to gate behaviour.

Heavy Alembic-only utilities (``assign_uuids``, schema migration helpers
…) are imported on demand by Alembic version files and are unrelated to
the runtime CLI surface.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Iterator, Optional, Union

from sqlalchemy import inspect
from sqlalchemy.orm import Query, Session

from superset.utils import json

# The BATCH_SIZE environment variable overrides the default of 1000.
DEFAULT_BATCH_SIZE = int(os.environ.get("BATCH_SIZE", 1000))


def paginated_update(
    query: Query[Any],
    print_page_progress: Optional[Union[Callable[[int, int], None], bool]] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[Any]:
    """Yield model instances in fixed-size batches.

    The sync :class:`~sqlalchemy.orm.Session` is resolved via
    :func:`sqlalchemy.inspect` (works for both ``Query`` objects and
    ``select()`` statements).
    """
    total = query.count()
    processed = 0
    session: Session = inspect(query).session  # type: ignore[union-attr]
    result = session.execute(query)

    if print_page_progress is None or print_page_progress is True:
        print_page_progress = lambda processed, total: print(  # noqa: E731
            f"    {processed}/{total}", end="\r"
        )

    while True:
        rows = result.fetchmany(batch_size)

        if not rows:
            break

        for row in rows:
            yield row[0]

        session.commit()
        processed += len(rows)

        if print_page_progress:
            print_page_progress(processed, total)


def try_load_json(data: Optional[str]) -> dict[str, Any]:
    """Load *data* as JSON, returning an empty dict on falsy / invalid input.

    Verbatim port of the upstream helper used by the chart migration
    pipeline to decode ``slice.params`` and ``slice.query_context``.
    """
    return data and json.loads(data) or {}


def has_table(table_name: str) -> bool:
    """Return whether *table_name* exists in the current Alembic context.

    Used by chart-migration processors to gate per-table transforms.
    Imports the Alembic ``op`` lazily so the helper is safe to call from
    non-Alembic contexts (returns ``False`` then).
    """
    try:
        from alembic import op

        insp = inspect(op.get_context().bind)
        return bool(insp.has_table(table_name))  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return False
