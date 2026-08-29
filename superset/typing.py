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
the concrete DAO classes (which would pull in the legacy import chain).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import IntEnum
from typing import Any, Literal, Optional, Protocol, runtime_checkable, TypedDict, Union

from sqlalchemy.sql.type_api import TypeEngine


class GenericDataType(IntEnum):
    """Generic database column type that fits both frontend and backend."""

    NUMERIC = 0
    STRING = 1
    TEMPORAL = 2
    BOOLEAN = 3


# Re-exported so ``from superset.typing import X`` keeps working for callers that
# previously used ``from superset.superset_typing import X``.
SQLType = Union[TypeEngine[Any], type[TypeEngine[Any]]]


class LegacyMetric(TypedDict):
    label: Optional[str]


class AdhocMetricColumn(TypedDict, total=False):
    column_name: Optional[str]
    description: Optional[str]
    expression: Optional[str]
    filterable: bool
    groupby: bool
    id: int
    is_dttm: bool
    python_date_format: Optional[str]
    type: str
    type_generic: "GenericDataType"
    verbose_name: Optional[str]


class AdhocMetric(TypedDict, total=False):
    aggregate: str
    column: Optional[AdhocMetricColumn]
    expressionType: Literal["SIMPLE", "SQL"]
    hasCustomLabel: Optional[bool]
    label: Optional[str]
    sqlExpression: Optional[str]


class AdhocColumn(TypedDict, total=False):
    hasCustomLabel: Optional[bool]
    label: str
    sqlExpression: str
    isColumnReference: Optional[bool]
    columnType: Optional[Literal["BASE_AXIS", "SERIES"]]
    timeGrain: Optional[str]


CacheConfig = dict[str, Any]

DbapiDescriptionRow = tuple[
    Union[str, bytes],
    str,
    Optional[str],
    Optional[str],
    Optional[int],
    Optional[int],
    bool,
]
DbapiDescription = Union[list[DbapiDescriptionRow], tuple[DbapiDescriptionRow, ...]]
DbapiResult = Sequence[Union[list[Any], tuple[Any, ...]]]

FilterValue = Union[bool, datetime, float, int, str]
FilterValues = Union[FilterValue, list[FilterValue], tuple[FilterValue, ...]]

FormData = dict[str, Any]
Granularity = Union[str, dict[str, Union[str, float]]]

Column = Union[AdhocColumn, str]
Metric = Union[AdhocMetric, str]
OrderBy = tuple[Union[Metric, Column], bool]

QueryObjectDict = dict[str, Any]
VizData = Optional[Union[list[Any], dict[Any, Any]]]
VizPayload = dict[str, Any]


class OAuth2ClientConfig(TypedDict):
    """Configuration for an OAuth2 client."""

    id: str
    secret: str
    scope: str
    redirect_uri: str
    authorization_request_uri: str
    token_request_uri: str
    request_content_type: str


class OAuth2TokenResponse(TypedDict, total=False):
    """Type for an OAuth2 response when exchanging or refreshing tokens."""

    access_token: str
    expires_in: int
    scope: str
    token_type: str
    refresh_token: str


class OAuth2State(TypedDict):
    """Type for the state passed during OAuth2."""

    database_id: int
    user_id: int
    default_redirect_uri: str
    tab_id: str


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
    async def find_by_id(self, model_id: int | str) -> Any: ...
    async def find_by_ids(self, model_ids: Sequence[int | str]) -> list[Any]: ...
    async def find_all(
        self,
        filters: list[Any] | None = None,
        page: int = 0,
        page_size: int = 0,
        order_by: list[Any] | None = None,
        options: list[Any] | None = None,
        joins: list[Any] | None = None,
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
    async def get_by_id_or_uuid(self, id_or_uuid: int | str) -> Any: ...
    async def find_by_id_with_options(
        self, chart_id: int, options: list[Any] | None = None
    ) -> Any: ...
    async def favorited_ids(self, obj_ids: list[int], user_id: int) -> list[int]: ...
    async def is_favorited_by(self, obj_id: int, user_id: int) -> bool: ...
    async def add_favorite(self, obj_id: int, user_id: int) -> None: ...
    async def remove_favorite(self, obj_id: int, user_id: int) -> None: ...


@runtime_checkable
class DashboardDAOProtocol(CRUDDAOProtocol, Protocol):
    async def get_by_id_or_slug(self, id_or_slug: int | str) -> Any: ...
    async def get_full_by_id_or_slug(
        self,
        id_or_slug: int | str,
        *,
        extra_filters: list[Any] | None = None,
    ) -> Any: ...
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
    async def find_with_filters_and_options(
        self, filters: list[Any], options: list[Any] | None = None
    ) -> Any: ...
    async def find_by_id_with_options(
        self, dashboard_id: int | Any, options: list[Any] | None = None
    ) -> Any: ...


@runtime_checkable
class DatabaseDAOProtocol(CRUDDAOProtocol, Protocol):
    async def get_ssh_tunnel(self, database_id: int) -> Any: ...
    async def validate_uniqueness(self, database_name: str) -> bool: ...
    async def get_related_objects(
        self,
        database_id: int,
        *,
        chart_filters: list[Any] | None = None,
        dashboard_filters: list[Any] | None = None,
    ) -> dict[str, Any]: ...
    async def validate_update_uniqueness(
        self, database_id: int, database_name: str
    ) -> bool: ...
    async def get_table_extra_lookup(
        self,
        database_id: int | Any,
        table_names: Any,
        schema: str | None = None,
    ) -> dict[str, dict[str, Any]]: ...


@runtime_checkable
class DatasetDAOProtocol(CRUDDAOProtocol, Protocol):
    async def validate_uniqueness(
        self,
        database_id: int,
        table_name: str,
        schema: str | None = None,
        catalog: str | None = None,
        dataset_id: int | None = None,
    ) -> bool: ...
    async def get_database_by_id(self, database_id: int) -> Any: ...
    async def get_related_objects(
        self,
        dataset_id: int,
        *,
        chart_filters: list[Any] | None = None,
        dashboard_filters: list[Any] | None = None,
    ) -> dict[str, list[Any]]: ...
    async def find_by_id_with_options(
        self, dataset_id: int, options: list[Any] | None = None
    ) -> Any: ...


@runtime_checkable
class EmbeddedDAOProtocol(CRUDDAOProtocol, Protocol):
    async def upsert(self, dashboard_id: int, allowed_domains: list[str]) -> Any: ...
    async def find_by_dashboard_id(self, dashboard_id: int) -> Any: ...


@runtime_checkable
class KeyValueDAOProtocol(Protocol):
    async def set_value(
        self,
        resource: str,
        resource_id: int,
        key: str,
        value: str,
        user_id: int | None = None,
        expires_on: datetime | None = None,
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
    async def stop_query(self, client_id: str) -> Any: ...
    async def get_queries_changed_after(
        self, user_id: int, last_updated_ms: float | int
    ) -> list[Any]: ...


@runtime_checkable
class ColumnDAOProtocol(CRUDDAOProtocol, Protocol):
    async def find_by_dataset_and_id(self, dataset_id: int, column_id: int) -> Any: ...


@runtime_checkable
class MetricDAOProtocol(CRUDDAOProtocol, Protocol):
    async def find_by_dataset_and_id(self, dataset_id: int, metric_id: int) -> Any: ...


@runtime_checkable
class DatasourceDAOProtocol(Protocol):
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
        *,
        catalog: str | None = None,
        hierarchical: bool = True,
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
        audience: str = "",
    ) -> str: ...


@runtime_checkable
class RoleDAOProtocol(Protocol):
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
    @property
    def id(self) -> int: ...

    @property
    def username(self) -> str: ...

    @property
    def is_authenticated(self) -> bool: ...

    @property
    def permissions(self) -> set[tuple[str, str]]: ...
