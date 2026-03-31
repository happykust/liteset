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
"""Helpers for loading Superset example datasets.

Adapted from the original ``superset/examples/helpers.py`` to work
without Flask.  Uses :mod:`superset.examples._ctx` for session/engine
access instead of ``from superset import db``.
"""
from __future__ import annotations

import os
import time
from typing import Any
from urllib.error import HTTPError

import pandas as pd

from superset.examples import _ctx
from superset.models.connectors import SqlaTable
from superset.models.slice import Slice
from superset.utils import json

EXAMPLES_PROTOCOL = "examples://"

# ---------------------------------------------------------------------------
# Public sample-data mirror configuration
# ---------------------------------------------------------------------------
BASE_COMMIT: str = os.getenv("SUPERSET_EXAMPLES_DATA_REF", "master")
BASE_URL: str = os.getenv(
    "SUPERSET_EXAMPLES_BASE_URL",
    f"https://cdn.jsdelivr.net/gh/apache-superset/examples-data@{BASE_COMMIT}/",
)

# Slices assembled into a 'Misc Chart' dashboard
misc_dash_slices: set[str] = set()


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def get_table_connector_registry() -> Any:
    return SqlaTable


def get_examples_folder() -> str:
    return os.path.join(_ctx.base_dir, "examples")


def update_slice_ids(pos: dict[Any, Any]) -> list[Slice]:
    """Update slice ids in ``position_json`` and return the slices found."""
    slice_components = [
        component
        for component in pos.values()
        if isinstance(component, dict) and component.get("type") == "CHART"
    ]
    slices: dict[str, Slice] = {}
    for name in {component["meta"]["sliceName"] for component in slice_components}:
        slc = _ctx.session.query(Slice).filter_by(slice_name=name).first()
        if slc:
            slices[name] = slc
    for component in slice_components:
        slc = slices.get(component["meta"]["sliceName"])
        if slc:
            component["meta"]["chartId"] = slc.id
            component["meta"]["uuid"] = str(slc.uuid)
    return list(slices.values())


def merge_slice(slc: Slice) -> None:
    """Upsert a Slice by name."""
    existing = (
        _ctx.session.query(Slice).filter_by(slice_name=slc.slice_name).first()
    )
    if existing:
        _ctx.session.delete(existing)
    _ctx.session.add(slc)


def get_slice_json(defaults: dict[Any, Any], **kwargs: Any) -> str:
    """Return JSON string for a chart definition."""
    defaults_copy = defaults.copy()
    defaults_copy.update(kwargs)
    return json.dumps(defaults_copy, indent=4, sort_keys=True)


def get_example_url(filepath: str) -> str:
    return f"{BASE_URL}{filepath}"


def normalize_example_data_url(url: str) -> str:
    if url.startswith(EXAMPLES_PROTOCOL):
        relative_path = url[len(EXAMPLES_PROTOCOL):]
        return get_example_url(relative_path)
    return url


def read_example_data(
    filepath: str,
    max_attempts: int = 5,
    wait_seconds: float = 60,
    **kwargs: Any,
) -> pd.DataFrame:
    """Load CSV or JSON from example data mirror with retry/backoff."""
    url = normalize_example_data_url(filepath)
    is_json = filepath.endswith(".json") or filepath.endswith(".json.gz")

    for attempt in range(1, max_attempts + 1):
        try:
            if is_json:
                return pd.read_json(url, **kwargs)
            return pd.read_csv(url, **kwargs)
        except HTTPError:
            if attempt < max_attempts:
                sleep_time = wait_seconds * (2 ** (attempt - 1))
                print(
                    f"HTTP error from {url}. "
                    f"Retrying in {sleep_time:.1f}s "
                    f"(attempt {attempt}/{max_attempts})..."
                )
                time.sleep(sleep_time)
            else:
                raise
    raise RuntimeError("Unreachable")  # pragma: no cover
