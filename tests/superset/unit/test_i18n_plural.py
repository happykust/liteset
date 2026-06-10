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
"""ngettext — locale-aware plural selection (Flask-Babel parity)."""

from __future__ import annotations

import superset.i18n as i18n


def setup_function() -> None:
    i18n._plural_tables.clear()
    i18n._plural_rules.clear()
    i18n._translations.pop("ru", None)


def teardown_function() -> None:
    setup_function()
    i18n.set_locale("en")


def test_ngettext_english_fallback() -> None:
    i18n.set_locale("en")
    assert (
        i18n.ngettext(
            "Deleted %(num)d report schedule", "Deleted %(num)d report schedules", num=1
        )
        == "Deleted 1 report schedule"
    )
    assert (
        i18n.ngettext(
            "Deleted %(num)d report schedule", "Deleted %(num)d report schedules", num=3
        )
        == "Deleted 3 report schedules"
    )


def test_ngettext_russian_three_forms() -> None:
    """Russian Plural-Forms rule resolves all three forms."""
    i18n.init_plural_data(
        {
            "ru": {
                "Deleted %(num)d report schedule": [
                    "Удалено %(num)d расписание",
                    "Удалено %(num)d расписания",
                    "Удалено %(num)d расписаний",
                ]
            }
        },
        {
            "ru": (
                "(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
                "(n%100<10 || n%100>=20) ? 1 : 2)"
            )
        },
    )
    i18n.set_locale("ru")
    n = i18n.ngettext
    s, p = "Deleted %(num)d report schedule", "Deleted %(num)d report schedules"
    assert n(s, p, num=1) == "Удалено 1 расписание"
    assert n(s, p, num=3) == "Удалено 3 расписания"
    assert n(s, p, num=7) == "Удалено 7 расписаний"
    assert n(s, p, num=21) == "Удалено 21 расписание"
