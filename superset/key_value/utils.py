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
"""Key-value store utility functions.

All functions are synchronous (pure computation, no I/O).
"""

from __future__ import annotations

from hashlib import md5
from secrets import token_urlsafe
from typing import Any
from uuid import UUID, uuid3

import hashids

from superset.i18n import gettext as _
from superset.key_value.exceptions import KeyValueParseKeyError
from superset.key_value.types import Key, KeyValueFilter, KeyValueResource
from superset.utils.json import dumps, json_int_dttm_ser

HASHIDS_MIN_LENGTH = 11


def json_dumps_w_dates(payload: dict[Any, Any], sort_keys: bool = False) -> str:
    """Dump payload to JSON with datetime objects converted to epoch millis."""
    return dumps(payload, default=json_int_dttm_ser, sort_keys=sort_keys)


def random_key(nbytes: int = 8) -> str:
    """Generate a random URL-safe string.

    :param nbytes: Number of bytes to use for generating the key.  Default
        is 8.
    """
    return token_urlsafe(nbytes)


def get_filter(resource: KeyValueResource, key: Key) -> KeyValueFilter:
    """Build a :class:`KeyValueFilter` dict for a resource + key pair.

    :param resource: The :class:`KeyValueResource` namespace.
    :param key: Either an integer ID or a :class:`~uuid.UUID`.
    :returns: A filter dict suitable for DAO lookup methods.
    :raises KeyValueParseKeyError: If the key cannot be parsed.
    """
    try:
        filter_: KeyValueFilter = {"resource": resource.value}
        if isinstance(key, UUID):
            filter_["uuid"] = key
        else:
            filter_["id"] = key
        return filter_
    except ValueError as ex:
        raise KeyValueParseKeyError() from ex


def encode_permalink_key(key: int, salt: str) -> str:
    """Encode an integer key into a short, URL-safe permalink hash.

    :param key: The integer primary key to encode.
    :param salt: A secret salt for the Hashids encoder.
    :returns: The encoded permalink string.
    """
    obj = hashids.Hashids(salt, min_length=HASHIDS_MIN_LENGTH)
    return obj.encode(key)


def decode_permalink_id(key: str, salt: str) -> int:
    """Decode a permalink hash back into the original integer key.

    :param key: The encoded permalink string.
    :param salt: The same secret salt used during encoding.
    :returns: The decoded integer primary key.
    :raises KeyValueParseKeyError: If the key cannot be decoded to exactly
        one integer.
    """
    obj = hashids.Hashids(salt, min_length=HASHIDS_MIN_LENGTH)
    ids = obj.decode(key)
    if len(ids) == 1:
        return ids[0]
    raise KeyValueParseKeyError(_("Invalid permalink key"))


def get_uuid_namespace(seed: str) -> UUID:
    """Derive a UUID namespace from a seed string via MD5 hashing.

    :param seed: Arbitrary string used to generate a reproducible UUID
        namespace.
    :returns: A :class:`~uuid.UUID` derived from the MD5 hash of *seed*.
    """
    md5_obj = md5()  # noqa: S324
    md5_obj.update(seed.encode("utf-8"))
    return UUID(md5_obj.hexdigest())


def get_deterministic_uuid(namespace: str, payload: Any) -> UUID:
    """Get a deterministic UUID (uuid3) from a salt and a JSON-serializable
    payload.

    :param namespace: A string seed used to derive the UUID namespace.
    :param payload: Any JSON-serializable object.
    :returns: A deterministic :class:`~uuid.UUID` (version 3).
    """
    payload_str = json_dumps_w_dates(payload, sort_keys=True)
    return uuid3(get_uuid_namespace(namespace), payload_str)
