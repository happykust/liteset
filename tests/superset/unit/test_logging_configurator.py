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
"""Flask-free port of ``tests/integration_tests/logging_configurator_tests.py``.

Verifies that a custom :class:`LoggingConfigurator` subclass can install
its own logging handler via :meth:`configure_logging` and that records
emitted afterwards flow through to it.
"""

import logging
from unittest.mock import MagicMock

from superset.utils.logging_configurator import LoggingConfigurator


def _reset_logging() -> None:
    # work around all of the import side-effects in superset
    logging.root.manager.loggerDict = {}
    logging.root.handlers = []
    # The integration suite ran under the fully-imported Superset app, whose
    # boot path lowers the root logger level so INFO records propagate. In
    # isolation the root logger defaults to WARNING, which would gate the
    # INFO record before it reaches the handler; restore the expected level.
    logging.root.setLevel(logging.DEBUG)


def test_configurator_adding_handler() -> None:
    class MyEventHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.received = False

        def handle(self, record: logging.LogRecord) -> None:
            if hasattr(record, "testattr"):
                self.received = True

    class MyConfigurator(LoggingConfigurator):
        def __init__(self, handler: logging.Handler) -> None:
            self.handler = handler

        def configure_logging(self, app_config, debug_mode):
            super().configure_logging(app_config, debug_mode)
            logging.getLogger().addHandler(self.handler)

    _reset_logging()

    handler = MyEventHandler()
    cfg = MyConfigurator(handler)
    cfg.configure_logging(MagicMock(), True)

    logging.info("test", extra={"testattr": "foo"})
    assert handler.received
