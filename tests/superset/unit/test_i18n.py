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
from superset.i18n import gettext, lazy_gettext, LazyString, set_locale


def test_gettext_default():
    set_locale("en")
    assert gettext("Hello") == "Hello"


def test_lazy_resolves_on_str():
    lazy = lazy_gettext("Hello")
    assert isinstance(lazy, LazyString)
    assert str(lazy) == "Hello"


def test_lazy_equality():
    assert lazy_gettext("X") == "X"


def test_lazy_hash():
    d = {lazy_gettext("key"): 1}
    assert d["key"] == 1


def test_lazy_concat():
    lazy = lazy_gettext("Hello")
    assert lazy + " world" == "Hello world"
    assert "Say " + lazy == "Say Hello"


def test_lazy_format():
    lazy = lazy_gettext("Hello %s")
    assert lazy % "world" == "Hello world"


def test_lazy_bool():
    assert bool(lazy_gettext("x")) is True


def test_lazy_len():
    assert len(lazy_gettext("abc")) == 3
