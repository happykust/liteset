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
"""Feature flag manager backed by SupersetSettings config.

Supports GET_FEATURE_FLAGS_FUNC (post-processes the whole flags dict per-request)
and IS_FEATURE_ENABLED_FUNC (overrides a single flag's resolution).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any


class FeatureFlagManager:
    def __init__(self) -> None:
        self._get_feature_flags_func: (
            Callable[[dict[str, bool]], dict[str, bool]] | None
        ) = None
        self._is_feature_enabled_func: Callable[[str, bool | None], bool] | None = None
        self._feature_flags: dict[str, bool] = {}

    def init_from_config(
        self,
        feature_flags: dict[str, bool],
        get_feature_flags_func: Any | None = None,
        is_feature_enabled_func: Any | None = None,
    ) -> None:
        """Initialise from config."""
        self._feature_flags = deepcopy(feature_flags)
        self._get_feature_flags_func = get_feature_flags_func
        self._is_feature_enabled_func = is_feature_enabled_func

    def get_feature_flags(self) -> dict[str, bool]:
        """Return the merged feature-flags dict.

        * If GET_FEATURE_FLAGS_FUNC is set, call it with a *deep copy* of the
          flags dict and return its result (the callback may mutate/return a
          modified copy — per-request / per-user overrides).
        * Else if IS_FEATURE_ENABLED_FUNC is callable, map it over every
          entry and return the resulting dict.
        * Else return the raw flags dict.
        """
        if self._get_feature_flags_func:
            return self._get_feature_flags_func(deepcopy(self._feature_flags))
        if callable(self._is_feature_enabled_func):
            return dict(  # noqa: C417
                map(
                    lambda kv: (kv[0], self._is_feature_enabled_func(kv[0], kv[1])),  # type: ignore[misc]
                    self._feature_flags.items(),
                )
            )
        return self._feature_flags

    def is_feature_enabled(self, feature: str) -> bool:
        """Return whether a feature flag is enabled.

        * If IS_FEATURE_ENABLED_FUNC is set, use it exclusively:
          call ``func(feature, current_value)`` when the flag exists,
          or return ``False`` when it does not.
        * Otherwise fall back to ``get_feature_flags()`` dict lookup,
          returning ``False`` if the flag is absent.
        """
        if self._is_feature_enabled_func:
            return (
                self._is_feature_enabled_func(feature, self._feature_flags[feature])
                if feature in self._feature_flags
                else False
            )
        feature_flags = self.get_feature_flags()
        if feature_flags and feature in feature_flags:
            return feature_flags[feature]
        return False


feature_flag_manager = FeatureFlagManager()
