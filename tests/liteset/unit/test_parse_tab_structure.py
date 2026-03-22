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
from liteset.commands.dashboard import parse_tab_structure


def test_empty_position_json():
    assert parse_tab_structure(None) == []
    assert parse_tab_structure("") == []


def test_invalid_json():
    assert parse_tab_structure("{invalid") == []


def test_no_tabs():
    assert parse_tab_structure('{"ROOT_ID": {"type": "ROOT"}}') == []


def test_single_tab():
    pos = '{"TAB-1": {"type": "TAB", "meta": {"text": "Tab 1"}, "parents": []}, "CHART-1": {"type": "CHART", "meta": {"chartId": 42}, "parents": ["TAB-1"]}}'
    tabs = parse_tab_structure(pos)
    assert len(tabs) == 1
    assert tabs[0]["tab_title"] == "Tab 1"
    assert 42 in tabs[0]["charts"]
