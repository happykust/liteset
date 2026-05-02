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
"""Encrypted field plumbing — ported 1:1 from ``superset_old/utils/encrypt.py``.

The original implementation depends on Flask via ``init_app(app)`` and
walks ``db.metadata.tables`` through the Flask-SQLAlchemy session.  This
module exposes the same API (``EncryptedFieldFactory.create``,
``SecretsMigrator.run``) but pulls configuration from
:class:`superset.config.SupersetSettings` and walks the metadata via a
synchronous engine built on top of the runtime async DSN — exactly the
same pattern used in :mod:`superset.utils.rls`.

Translatable error messages use :func:`superset.i18n.lazy_gettext`.
"""

from __future__ import annotations

import functools
import logging
from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy import create_engine, MetaData, text, TypeDecorator
from sqlalchemy.engine import Connection, Dialect, Engine
from sqlalchemy_utils import EncryptedType as SqlaEncryptedType

from superset.i18n import lazy_gettext as _

logger = logging.getLogger(__name__)

ENC_ADAPTER_TAG_ATTR_NAME = "__created_by_enc_field_adapter__"


# ---------------------------------------------------------------------------
# EncryptedType — sqlalchemy_utils subclass with ``cache_ok = True``
# (mirrors the original).
# ---------------------------------------------------------------------------


class EncryptedType(SqlaEncryptedType):
    cache_ok = True


# ---------------------------------------------------------------------------
# Adapter abstraction
# ---------------------------------------------------------------------------


class AbstractEncryptedFieldAdapter(ABC):  # pylint: disable=too-few-public-methods
    """Strategy interface for constructing encrypted SA column types."""

    @abstractmethod
    def create(
        self,
        secret_key: str | bytes | None,
        *args: Any,
        **kwargs: Any,
    ) -> TypeDecorator[Any]: ...


class SQLAlchemyUtilsAdapter(  # pylint: disable=too-few-public-methods
    AbstractEncryptedFieldAdapter
):
    """Default adapter — wraps :class:`EncryptedType` from sqlalchemy_utils.

    Mirrors the original ``SQLAlchemyUtilsAdapter`` exactly.  The ``app_config``
    parameter has been collapsed to a plain ``secret_key`` argument now that
    we no longer depend on Flask's app context.
    """

    def create(
        self,
        secret_key: str | bytes | None,
        *args: Any,
        **kwargs: Any,
    ) -> TypeDecorator[Any]:
        if secret_key is None:
            raise ValueError("Missing secret_key for encrypted field")
        return EncryptedType(*args, secret_key, **kwargs)


# ---------------------------------------------------------------------------
# EncryptedFieldFactory — full port (without Flask)
# ---------------------------------------------------------------------------


def _resolve_settings() -> Any:
    """Return a cached :class:`SupersetSettings` instance.

    Importing the settings is deferred so that this module can be loaded
    before the application's environment variables are populated (mirrors
    the late-binding behaviour of Flask's ``init_app``).
    """
    return _cached_settings()


@functools.lru_cache(maxsize=1)
def _cached_settings() -> Any:
    from superset.config import SupersetSettings

    return SupersetSettings()  # type: ignore[call-arg]


def _resolve_secret_key(settings: Any) -> str | bytes:
    """Coerce ``settings.secret_key`` (Pydantic ``SecretStr``) to a plain string."""
    raw = settings.secret_key
    if hasattr(raw, "get_secret_value"):
        return raw.get_secret_value()
    return raw


class EncryptedFieldFactory:
    """Builds encrypted SA column types using the configured adapter.

    Equivalent to the original Flask-aware ``EncryptedFieldFactory`` but
    initialised lazily from :class:`SupersetSettings`.  ``init_app`` is
    kept for backward compatibility with code that still passes a Litestar
    application object (or any object exposing a ``state.settings``).
    """

    def __init__(
        self,
        secret_key: str | bytes | None = None,
        adapter: AbstractEncryptedFieldAdapter | None = None,
    ) -> None:
        self._secret_key: str | bytes | None = secret_key
        self._adapter: AbstractEncryptedFieldAdapter | None = adapter
        self._initialised = secret_key is not None and adapter is not None

    # --- initialisation hooks ------------------------------------------------
    def init_app(self, app: Any | None = None) -> None:
        """Configure the factory from :class:`SupersetSettings`.

        ``app`` is accepted for backward compatibility (callers from
        :func:`superset.app.on_startup` pass the Litestar instance) but
        the configuration is always read from the pydantic settings.
        """
        del app  # unused — kept for API compatibility
        settings = _resolve_settings()
        self._secret_key = _resolve_secret_key(settings)

        adapter_cls = settings.sqlalchemy_encrypted_field_type_adapter
        if adapter_cls is None:
            self._adapter = SQLAlchemyUtilsAdapter()
        else:
            try:
                self._adapter = adapter_cls()
            except TypeError:
                # The original config exposes an *instance*; tolerate that too.
                self._adapter = adapter_cls
        self._initialised = True

    # --- column-type construction -------------------------------------------
    def create(self, *args: Any, **kwargs: Any) -> TypeDecorator[Any]:
        """Return a new encrypted SA column type.

        If the factory has not been explicitly initialised this performs a
        late ``init_app`` from settings.  This matches the original
        behaviour where the first model import after ``init_app(app)``
        triggers adapter creation.
        """
        if not self._initialised:
            self.init_app()
        assert self._adapter is not None  # noqa: S101 — guaranteed by init_app
        adapter_field = self._adapter.create(self._secret_key, *args, **kwargs)
        setattr(adapter_field, ENC_ADAPTER_TAG_ATTR_NAME, True)
        return adapter_field

    @staticmethod
    def created_by_enc_field_factory(field: TypeDecorator[Any]) -> bool:
        return getattr(field, ENC_ADAPTER_TAG_ATTR_NAME, False)


# ---------------------------------------------------------------------------
# SecretsMigrator — re-encrypts every EncryptedType column with a new key.
#
# Uses a *sync* engine (mirrors ``utils.rls._metadata_sync_engine``)
# because re-encryption is a one-shot CLI operation that runs outside any
# Litestar request context.
# ---------------------------------------------------------------------------


_ASYNC_TO_SYNC_DRIVERS: dict[str, str] = {
    "postgresql+asyncpg://": "postgresql+psycopg2://",
    "mysql+aiomysql://": "mysql+pymysql://",
    "mysql+asyncmy://": "mysql+pymysql://",
    "sqlite+aiosqlite://": "sqlite://",
}


def _to_sync_uri(uri: str) -> str:
    """Convert an async SQLAlchemy URI to its sync equivalent.

    Required because :func:`create_engine` (sync) refuses async drivers
    such as ``asyncpg`` with ``InvalidArgumentError: The asyncio extension
    requires an async driver``.
    """
    for src, dst in _ASYNC_TO_SYNC_DRIVERS.items():
        if uri.startswith(src):
            return uri.replace(src, dst, 1)
    return uri


@functools.lru_cache(maxsize=1)
def _metadata_sync_engine() -> Engine:
    """Return a process-wide cached sync engine for the metadata DB."""
    settings = _resolve_settings()
    sync_uri = _to_sync_uri(str(settings.sqlalchemy_database_uri))
    return create_engine(sync_uri)


class SecretsMigrator:
    """Walks every metadata table and re-encrypts every ``EncryptedType``
    column under the new ``SECRET_KEY``.

    Direct port of ``superset_old/utils/encrypt.py:SecretsMigrator``; the
    only behaviour change is that we no longer depend on Flask-SQLAlchemy
    (we open a sync engine ourselves) and we read both the previous *and*
    the new key from arguments / settings instead of ``app.config``.
    """

    def __init__(
        self,
        previous_secret_key: str,
        new_secret_key: str | bytes | None = None,
    ) -> None:
        # The *new* secret key — defaults to whatever
        # :class:`SupersetSettings` resolves on import.  The migrator uses
        # it to construct fresh ``EncryptedType`` instances that bind the
        # new key as the encryption key.
        if new_secret_key is None:
            new_secret_key = _resolve_secret_key(_resolve_settings())
        self._previous_secret_key = previous_secret_key
        self._new_secret_key = new_secret_key

        self._engine: Engine = _metadata_sync_engine()
        # ``get_dialect()`` returns the dialect *class* (not an instance) —
        # mirrors ``superset_old/utils/encrypt.py``.  ``EncryptedType``'s
        # ``process_bind_param`` / ``process_result_value`` accept the
        # class directly (they only call ``.name`` on it), and the original
        # code never instantiated the dialect either.
        self._dialect: type[Dialect] = self._engine.url.get_dialect()

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    def discover_encrypted_fields(self) -> dict[str, dict[str, EncryptedType]]:
        """Return a ``{table_name: {col_name: encrypted_type}}`` mapping.

        Walks SQLAlchemy's *declarative* metadata (every model registered
        on :class:`superset.models.helpers.Base`) and selects only columns
        whose declared type subclasses :class:`EncryptedType`.
        """
        from superset.models.helpers import Base  # noqa: PLC0415

        meta_info: dict[str, dict[str, EncryptedType]] = {}
        metadata: MetaData = Base.metadata
        for table_name, table in metadata.tables.items():
            for col_name, col in table.columns.items():
                if isinstance(col.type, EncryptedType):
                    cols = meta_info.get(table_name, {})
                    cols[col_name] = col.type
                    meta_info[table_name] = cols
        return meta_info

    # ------------------------------------------------------------------
    # value coercion (1:1 port)
    # ------------------------------------------------------------------
    @staticmethod
    def _read_bytes(col_name: str, value: Any) -> bytes | None:
        if value is None or isinstance(value, bytes):
            return value
        # Postgres returns memoryviews for BLOB columns.
        if isinstance(value, memoryview):
            return value.tobytes()
        if isinstance(value, str):
            return bytes(value.encode("utf8"))
        raise ValueError(
            _(
                "DB column %(col_name)s has unknown type: %(value_type)s",
                col_name=col_name,
                value_type=type(value),
            )
        )

    # ------------------------------------------------------------------
    # SQL helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _select_columns_from_table(
        conn: Connection, column_names: list[str], table_name: str
    ) -> Any:
        # ``id`` is hard-coded because every encrypted-bearing model in
        # Superset has an ``id`` PK (mirrors the original).
        column_list = ", ".join(column_names)
        return conn.execute(
            text(f"SELECT id, {column_list} FROM {table_name}")  # noqa: S608
        )

    def _re_encrypt_row(
        self,
        conn: Connection,
        row: Any,
        table_name: str,
        columns: dict[str, EncryptedType],
    ) -> None:
        """Decrypt every encrypted column with the *previous* key and
        re-encrypt with the *new* key, then ``UPDATE`` the row.
        """
        re_encrypted_columns: dict[str, Any] = {}

        for column_name, encrypted_type in columns.items():
            previous_encrypted_type = EncryptedType(
                type_in=encrypted_type.underlying_type,
                key=self._previous_secret_key,
            )
            try:
                unencrypted_value = previous_encrypted_type.process_result_value(
                    self._read_bytes(column_name, row._mapping[column_name]),
                    self._dialect,
                )
            except ValueError as ex:
                # Decryption with the *previous* key failed; check if the
                # current key already decrypts the value (idempotent rerun).
                try:
                    encrypted_type.process_result_value(
                        self._read_bytes(column_name, row._mapping[column_name]),
                        self._dialect,
                    )
                    logger.info(
                        "Current secret is able to decrypt value on column "
                        "[%s.%s], nothing to do",
                        table_name,
                        column_name,
                    )
                    return
                except Exception as nested_ex:
                    raise Exception(  # pylint: disable=broad-exception-raised
                        f"Failed to decrypt {table_name}.{column_name}: {nested_ex}"
                    ) from ex

            new_encrypted_type = EncryptedType(
                type_in=encrypted_type.underlying_type,
                key=self._new_secret_key,
            )
            re_encrypted_columns[column_name] = new_encrypted_type.process_bind_param(
                unencrypted_value, self._dialect
            )

        set_cols = ", ".join(f"{name} = :{name}" for name in re_encrypted_columns)
        logger.info("Processing table: %s", table_name)
        conn.execute(
            text(
                f"UPDATE {table_name} SET {set_cols} WHERE id = :id"  # noqa: S608
            ),
            {"id": row._mapping["id"], **re_encrypted_columns},
        )

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Re-encrypt every ``EncryptedType`` column under the new key."""
        encrypted_meta_info = self.discover_encrypted_fields()

        with self._engine.begin() as conn:
            logger.info("Collecting info for re encryption")
            for table_name, columns in encrypted_meta_info.items():
                column_names = list(columns.keys())
                rows = self._select_columns_from_table(
                    conn, column_names, table_name
                ).fetchall()

                for row in rows:
                    self._re_encrypt_row(conn, row, table_name, columns)
        logger.info("All tables processed")


__all__ = [
    "AbstractEncryptedFieldAdapter",
    "EncryptedFieldFactory",
    "EncryptedType",
    "ENC_ADAPTER_TAG_ATTR_NAME",
    "SecretsMigrator",
    "SQLAlchemyUtilsAdapter",
]
