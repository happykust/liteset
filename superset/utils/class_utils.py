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
"""Utilities for resolving classes from fully-qualified names.

Ported 1:1 from ``superset_old/utils/class_utils.py``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


def load_class_from_name(fq_class_name: str) -> Any:
    """Load a class from a fully-qualified dotted path.

    :param fq_class_name: ``"package.module.ClassName"``
    :return: the class object (or any attribute reachable via the path)
    :raises ValueError: if ``fq_class_name`` is empty / falsy
    :raises ImportError: if the module cannot be imported
    :raises AttributeError: if the class is not in the resolved module
    """
    if not fq_class_name:
        raise ValueError(f"Invalid class name {fq_class_name}")

    parts = fq_class_name.split(".")
    module_name = ".".join(parts[:-1])
    class_name = parts[-1]

    module = import_module(module_name)
    return getattr(module, class_name)
