from __future__ import annotations

from typing import Any

import msgspec


class ApiResponse(msgspec.Struct):
    result: Any = None
    id: int | str | None = None
    message: str | None = None


class ApiListResponse(msgspec.Struct):
    result: list[Any] = []
    count: int = 0
    ids: list[int | str] = []
    label_columns: dict[str, str] = {}
    list_columns: list[str] = []
    order_columns: list[str] = []
    description_columns: dict[str, str] = {}


class ErrorResponse(msgspec.Struct):
    message: str = ""
    errors: dict[str, list[str]] = {}
    status: int = 400
