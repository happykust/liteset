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
"""Ported from ``tests/integration_tests/utils/encrypt.py``.

Exercises the encrypted-field plumbing in :mod:`superset.utils.encrypt`:
the :class:`EncryptedFieldFactory` adapter mechanism and the
:class:`SecretsMigrator` discovery sweep that guarantees every encrypted
column in the metadata was created via the factory.

The Liteset port differs from upstream in two ways that the test adapts to:

* there is no Flask ``app`` / ``app.config`` — the factory is configured by
  ``secret_key`` + ``adapter`` (read from
  :class:`superset.config.SupersetSettings` when not passed explicitly), so
  the tests build factories directly with an explicit key/adapter instead of
  mutating ``app.config`` and calling ``init_app(app)``;
* :class:`superset.utils.encrypt.EncryptedType` is a thin ``cache_ok``
  subclass of ``sqlalchemy_utils.EncryptedType``; the factory's default
  adapter produces that subclass, which is still an instance of the upstream
  ``EncryptedType`` (matching the original assertion).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, TypeDecorator
from sqlalchemy_utils import EncryptedType
from sqlalchemy_utils.types.encrypted.encrypted_type import StringEncryptedType

from superset.utils.encrypt import (
    AbstractEncryptedFieldAdapter,
    EncryptedFieldFactory,
    SecretsMigrator,
    SQLAlchemyUtilsAdapter,
)

SECRET_KEY = "test-secret-key-at-least-32-bytes-long-for-gaq"


class CustomEncFieldAdapter(AbstractEncryptedFieldAdapter):
    def create(
        self,
        secret_key: str | bytes | None,
        *args: Any,
        **kwargs: Any,
    ) -> TypeDecorator:
        if secret_key:
            return StringEncryptedType(*args, secret_key, **kwargs)
        raise Exception("Missing secret_key kwarg")


def test_create_field():
    factory = EncryptedFieldFactory(
        secret_key=SECRET_KEY, adapter=SQLAlchemyUtilsAdapter()
    )
    field = factory.create(String(1024))
    assert isinstance(field, EncryptedType)
    assert SECRET_KEY == field.key


def test_custom_adapter():
    factory = EncryptedFieldFactory(
        secret_key=SECRET_KEY, adapter=CustomEncFieldAdapter()
    )
    field = factory.create(String(1024))
    assert isinstance(field, StringEncryptedType)
    # The default adapter's subclass is *not* produced by the custom adapter.
    from superset.utils.encrypt import EncryptedType as SupersetEncryptedType

    assert not isinstance(field, SupersetEncryptedType)
    assert field.__created_by_enc_field_adapter__
    assert SECRET_KEY == field.key


def test_ensure_encrypted_field_factory_is_used():
    """
    Ensure that the EncryptedFieldFactory is used everywhere
    that an encrypted field is needed.
    """
    from superset.extensions import encrypted_field_factory

    migrator = SecretsMigrator("")
    encrypted_fields = migrator.discover_encrypted_fields()
    for table_name, cols in encrypted_fields.items():
        for col_name, field in cols.items():
            assert encrypted_field_factory.created_by_enc_field_factory(field), (
                f"The encrypted column [{col_name}]"
                f" in the table [{table_name}]"
                " was not created using the encrypted_field_factory"
            )
