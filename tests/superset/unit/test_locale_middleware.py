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
"""``Accept-Language`` / cookie resolution, bounded by configured LANGUAGES.

The resolver only ever returns a *configured* language — 1:1 with upstream's
``accept_languages.best_match(appbuilder.bm.languages)``.  With the default
English-only config a French/Chinese browser resolves to ``en`` (no match),
never a half-translated UI.
"""

from superset.middleware.locale import (
    _best_match,
    _extract_cookie_locale,
    _match_allowed,
)

# Multi-language deployment (deployer enabled these).
MULTI = {"en", "fr", "de", "ru", "pt_BR"}
# Default Superset deployment: English only.
ENGLISH_ONLY = {"en"}


def test_best_match_simple():
    assert _best_match("en", MULTI) == "en"


def test_best_match_with_region():
    assert _best_match("en-US", MULTI) == "en"


def test_best_match_quality_picks_highest_q():
    # fr has the higher q -> fr (and fr is enabled).
    assert _best_match("fr-FR;q=0.9, en;q=0.8", MULTI) == "fr"


def test_best_match_multiple():
    assert _best_match("de, en;q=0.5", MULTI) == "de"


def test_best_match_russian():
    assert _best_match("ru-RU,ru;q=0.9,en;q=0.8", MULTI) == "ru"


def test_best_match_region_specific_key():
    # pt-BR maps to the configured pt_BR key.
    assert _best_match("pt-BR", MULTI) == "pt_BR"


def test_best_match_falls_through_to_lower_q_when_top_not_enabled():
    # zh not enabled -> skip; en;q=0.5 is enabled -> en.
    assert _best_match("zh-CN,en;q=0.5", MULTI) == "en"


def test_best_match_english_only_ignores_foreign_browser():
    # The core parity fix: French/Chinese browser, English-only config -> no
    # match (caller then falls back to BABEL_DEFAULT_LOCALE, i.e. "en").
    assert _best_match("fr-FR,fr;q=0.9", ENGLISH_ONLY) is None
    assert _best_match("zh-CN", ENGLISH_ONLY) is None


def test_best_match_empty_header_or_allowed():
    assert _best_match("", MULTI) is None
    assert _best_match("fr", set()) is None


def test_match_allowed_case_insensitive():
    assert _match_allowed("FR", MULTI) == "fr"


def test_cookie_locale_must_be_allowed():
    raw = b"language=fr; other=1"
    assert _extract_cookie_locale(raw, "language", MULTI) == "fr"
    # Not in the English-only config -> ignored.
    assert _extract_cookie_locale(raw, "language", ENGLISH_ONLY) is None


def test_cookie_locale_absent():
    assert _extract_cookie_locale(b"other=1", "language", MULTI) is None
    assert _extract_cookie_locale(b"", "language", MULTI) is None
