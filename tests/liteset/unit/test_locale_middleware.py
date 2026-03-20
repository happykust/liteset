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
from liteset.middleware.locale import _parse_accept_language


def test_parse_simple():
    assert _parse_accept_language("en") == "en"


def test_parse_with_region():
    assert _parse_accept_language("en-US") == "en"


def test_parse_with_quality():
    assert _parse_accept_language("fr-FR;q=0.9, en;q=0.8") == "fr"


def test_parse_multiple():
    assert _parse_accept_language("de, en;q=0.5") == "de"


def test_parse_empty():
    assert _parse_accept_language("") == "en"


def test_parse_russian():
    assert _parse_accept_language("ru-RU,ru;q=0.9,en;q=0.8") == "ru"
