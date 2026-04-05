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
"""Protocol types for Litestar controller DI parameters.

Litestar resolves handler parameter types at runtime via get_type_hints().
TYPE_CHECKING imports crash because the types aren't available at runtime.
Protocol types solve this: they define the interface without importing
the concrete DAO classes (which would pull in the Flask import chain).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable


class GenericDataType(IntEnum):
    """Generic database column type that fits both frontend and backend."""

    NUMERIC = 0
    STRING = 1
    TEMPORAL = 2
    BOOLEAN = 3


@runtime_checkable
class DatasourceProtocol(Protocol):
    """Protocol for datasource objects passed to AsyncQueryContextProcessor."""

    @property
    def uid(self) -> str: ...

    @property
    def id(self) -> int: ...

    @property
    def type(self) -> str: ...

    @property
    def changed_on(self) -> Any: ...

    @property
    def column_names(self) -> list[str]: ...

    @property
    def offset(self) -> int: ...

    def get_extra_cache_keys(self, query_dict: dict[str, Any]) -> list[str]: ...

    def get_column(self, column_name: str | None) -> Any: ...

    def query(self, query_dict: dict[str, Any]) -> Any: ...


@runtime_checkable
class CRUDDAOProtocol(Protocol):
    """Protocol for standard CRUD DAO operations."""

    async def find_by_id(self, model_id: int | str) -> Any: ...
    async def find_by_ids(self, model_ids: Sequence[int | str]) -> list[Any]: ...
    async def find_all(
        self,
        filters: list[Any] | None = None,
        page: int = 0,
        page_size: int = 0,
        order_by: list[Any] | None = None,
        options: list[Any] | None = None,
    ) -> list[Any]: ...
    async def count(self, filters: list[Any] | None = None) -> int: ...
    async def find_one_or_none(self, **filter_by: Any) -> Any: ...
    async def create(self, attributes: dict[str, Any]) -> Any: ...
    async def update(self, item: Any, attributes: dict[str, Any]) -> Any: ...
    async def delete(self, items: list[Any]) -> None: ...
    async def bulk_delete(self, ids: list[int | str]) -> int: ...

    @property
    def session(self) -> Any: ...


@runtime_checkable
class ChartDAOProtocol(CRUDDAOProtocol, Protocol):
    """Protocol for AsyncChartDAO."""

    async def get_by_id_or_uuid(self, id_or_uuid: int | str) -> Any: ...
    async def favorited_ids(self, obj_ids: list[int], user_id: int) -> list[int]: ...
    async def is_favorited_by(self, obj_id: int, user_id: int) -> bool: ...
    async def add_favorite(self, obj_id: int, user_id: int) -> None: ...
    async def remove_favorite(self, obj_id: int, user_id: int) -> None: ...


@runtime_checkable
class DashboardDAOProtocol(CRUDDAOProtocol, Protocol):
    """Protocol for AsyncDashboardDAO."""

    async def get_by_id_or_slug(self, id_or_slug: int | str) -> Any: ...
    async def favorited_ids(self, obj_ids: list[int], user_id: int) -> list[int]: ...
    async def is_favorited_by(self, obj_id: int, user_id: int) -> bool: ...
    async def add_favorite(self, obj_id: int, user_id: int) -> None: ...
    async def remove_favorite(self, obj_id: int, user_id: int) -> None: ...
    async def get_datasets_for_dashboard(self, dashboard: Any) -> list[Any]: ...
    async def get_charts_for_dashboard(self, dashboard: Any) -> list[Any]: ...
    async def copy_dashboard(
        self, original_dash: Any, data: dict[str, Any], current_user: Any | None = None
    ) -> Any: ...
    async def update_colors_config(
        self, dashboard: Any, data: dict[str, Any]
    ) -> None: ...
    async def validate_slug_uniqueness(self, slug: str) -> bool: ...
    async def validate_update_slug_uniqueness(
        self, dashboard_id: int, slug: str | None
    ) -> bool: ...


@runtime_checkable
class DatabaseDAOProtocol(CRUDDAOProtocol, Protocol):
    """Protocol for AsyncDatabaseDAO."""

    async def get_ssh_tunnel(self, database_id: int) -> Any: ...
    async def validate_uniqueness(self, database_name: str) -> bool: ...
    async def get_related_objects(self, database_id: int) -> dict[str, Any]: ...
    async def validate_update_uniqueness(
        self, database_id: int, database_name: str
    ) -> bool: ...


@runtime_checkable
class DatasetDAOProtocol(CRUDDAOProtocol, Protocol):
    """Protocol for AsyncDatasetDAO."""

    async def validate_uniqueness(
        self,
        database_id: int,
        table_name: str,
        schema: str | None = None,
        catalog: str | None = None,
        dataset_id: int | None = None,
    ) -> bool: ...
    async def get_database_by_id(self, database_id: int) -> Any: ...
    async def get_related_objects(self, dataset_id: int) -> dict[str, list[Any]]: ...


@runtime_checkable
class EmbeddedDAOProtocol(CRUDDAOProtocol, Protocol):
    """Protocol for AsyncEmbeddedDashboardDAO."""

    async def upsert(self, dashboard_id: int, allowed_domains: list[str]) -> Any: ...
    async def find_by_dashboard_id(self, dashboard_id: int) -> Any: ...


@runtime_checkable
class KeyValueDAOProtocol(Protocol):
    """Protocol for AsyncKeyValueDAO."""

    async def set_value(
        self,
        resource: str,
        resource_id: int,
        key: str,
        value: str,
    ) -> None: ...
    async def get_value(
        self,
        resource: str,
        resource_id: int,
        key: str,
    ) -> str | None: ...
    async def delete_value(
        self,
        resource: str,
        resource_id: int,
        key: str,
    ) -> bool: ...


@runtime_checkable
class QueryDAOProtocol(CRUDDAOProtocol, Protocol):
    """Protocol for AsyncQueryDAO."""

    async def stop_query(self, client_id: str) -> Any: ...
    async def get_queries_changed_after(
        self, user_id: int, last_updated_ms: float | int
    ) -> list[Any]: ...


@runtime_checkable
class ColumnDAOProtocol(CRUDDAOProtocol, Protocol):
    """Protocol for AsyncDatasetColumnDAO."""

    async def find_by_dataset_and_id(self, dataset_id: int, column_id: int) -> Any: ...


@runtime_checkable
class MetricDAOProtocol(CRUDDAOProtocol, Protocol):
    """Protocol for AsyncDatasetMetricDAO."""

    async def find_by_dataset_and_id(self, dataset_id: int, metric_id: int) -> Any: ...


@runtime_checkable
class DatasourceDAOProtocol(Protocol):
    """Protocol for AsyncDatasourceDAO."""

    async def get_datasource(
        self, datasource_type: str, datasource_id: int
    ) -> Any | None: ...


@runtime_checkable
class SecurityManagerProtocol(Protocol):
    """Protocol for AsyncSecurityManager in DI contexts."""

    async def raise_for_access(
        self, *, user: Any, datasource: Any | None = None, **kwargs: Any
    ) -> None: ...

    async def get_rls_cache_key(self, datasource: Any, *, user: Any) -> list[str]: ...

    async def can_access_all_queries(self, *, user: Any) -> bool: ...

    async def can_access(
        self, permission_name: str, view_name: str, *, user: Any
    ) -> bool: ...

    async def find_user_by_id(self, user_id: int) -> Any | None: ...

    async def find_role_by_id(self, role_id: int) -> Any | None: ...

    def is_guest_user(self, user: Any) -> bool: ...

    async def get_catalogs_accessible_by_user(
        self,
        database: Any,
        catalog_names: list[str],
        *,
        user: Any,
    ) -> list[str]: ...

    async def get_schemas_accessible_by_user(
        self,
        database: Any,
        schema_names: list[str],
        catalog: str | None = None,
        *,
        user: Any,
    ) -> list[str]: ...

    @staticmethod
    def create_guest_access_token(
        *,
        secret_key: str,
        user: dict[str, Any],
        resources: list[dict[str, Any]],
        rls: list[dict[str, Any]],
        algorithm: str = "HS256",
        exp_seconds: int = 300,
    ) -> str: ...


@runtime_checkable
class RoleDAOProtocol(Protocol):
    """Protocol for AsyncRoleDAO."""

    async def search(
        self,
        name_filter: str | None = None,
        order_column: str = "id",
        order_direction: str = "asc",
        page: int = 0,
        page_size: int = 10,
    ) -> tuple[list[Any], int]: ...


@runtime_checkable
class UserProtocol(Protocol):
    """Protocol for the current_user dependency."""

    @property
    def id(self) -> int: ...

    @property
    def username(self) -> str: ...

    @property
    def is_authenticated(self) -> bool: ...

    @property
    def permissions(self) -> set[str]: ...
