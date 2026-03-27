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
from superset.dependencies import RequestCache


async def test_get_or_set_calls_factory_once():
    cache = RequestCache()
    call_count = 0

    async def factory():
        nonlocal call_count
        call_count += 1
        return "value"

    r1 = await cache.get_or_set("key", factory)
    r2 = await cache.get_or_set("key", factory)
    assert r1 == "value"
    assert r2 == "value"
    assert call_count == 1


def test_get_set():
    cache = RequestCache()
    assert cache.get("x") is None
    assert cache.get("x", "default") == "default"
    cache.set("x", 42)
    assert cache.get("x") == 42
