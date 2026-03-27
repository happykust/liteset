"""Minimal Jinja template processor for SQL."""
from __future__ import annotations

from typing import Any

from jinja2 import Environment

from superset.utils.feature_flags import feature_flag_manager


class NoOpTemplateProcessor:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def process_template(self, sql: str, **kwargs: Any) -> str:
        return sql


class JinjaTemplateProcessor:
    def __init__(self, database: Any = None, **kwargs: Any) -> None:
        self._env = Environment()

    def process_template(self, sql: str, **kwargs: Any) -> str:
        template = self._env.from_string(sql)
        return template.render(**kwargs)


def get_template_processor(
    database: Any = None, **kwargs: Any
) -> NoOpTemplateProcessor | JinjaTemplateProcessor:
    if feature_flag_manager.is_feature_enabled("ENABLE_TEMPLATE_PROCESSING"):
        return JinjaTemplateProcessor(database=database, **kwargs)
    return NoOpTemplateProcessor(**kwargs)
