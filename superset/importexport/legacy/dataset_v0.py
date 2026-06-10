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
"""Legacy (v0) dataset importer.

Direct port of ``superset_old/commands/dataset/importers/v0.py``.

v0 datasets are exported as YAML files containing either:

* a top-level ``databases`` key (CLI ``superset export_datasources``)
  whose value is a list of database dicts, each carrying ``tables``
  with ``columns`` and ``metrics``;
* a list of dataset dicts (UI export — each entry is a single
  ``SqlaTable`` row including a ``params`` JSON blob with the parent
  database's name).

Because the original :class:`~superset.models.helpers.ImportExportMixin`
no longer ships ``import_from_dict``, ``alter_params``, ``override`` and
related helpers, the equivalent generic algorithm is reimplemented
locally in this module.  The logic mirrors the original 1:1 — same
unique-constraint discovery, same parent-FK rewriting, same recursive
child handling, same ``sync`` semantics.

The command runs synchronously against a regular SQLAlchemy ``Session``
because v0 imports are exclusively driven from CLI / Celery contexts.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import yaml
from sqlalchemy import and_, or_, UniqueConstraint
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.orm.session import make_transient

from superset.commands.importers.exceptions import IncorrectVersionError
from superset.exceptions import CommandInvalidError
from superset.utils import json
from superset.utils.dict_import_export import DATABASES_KEY

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Generic import_from_dict — reimplementation of ``ImportExportMixin``
# ---------------------------------------------------------------------------


def _unique_constraints(cls: type) -> list[set[str]]:
    """Return every (single- and multi-column) unique constraint as a set.

    Mirrors :func:`ImportExportMixin._unique_constraints` from the
    original ``superset_old/models/helpers.py``.
    """
    unique: list[set[str]] = []
    table_args = getattr(cls, "__table_args__", ()) or ()
    if isinstance(table_args, dict):
        table_args = ()
    for u in table_args:
        if isinstance(u, UniqueConstraint):
            unique.append({c.name for c in u.columns})
    for c in cls.__table__.columns:  # type: ignore[attr-defined]
        if c.unique:
            unique.append({c.name})
    return unique


def _parent_foreign_key_mappings(cls: type) -> dict[str, str]:
    """Map ``local_fk_name -> remote_pk_name`` for the parent relationship.

    Mirrors :func:`ImportExportMixin.parent_foreign_key_mappings` 1:1.
    """
    parent_attr = getattr(cls, "export_parent", None)
    if not parent_attr:
        return {}
    parent_rel = cls.__mapper__.relationships.get(parent_attr)  # type: ignore[attr-defined]
    if not parent_rel:
        return {}
    return {
        local.name: remote.name for (local, remote) in parent_rel.local_remote_pairs
    }


def import_from_dict(  # noqa: C901
    session: Any,
    cls: type,
    dict_rep: dict[str, Any],
    parent: Any | None = None,
    recursive: bool = True,
    sync: list[str] | None = None,
    allow_reparenting: bool = False,
) -> Any:
    """Import a model row from a dict, mirroring the original mixin.

    Direct port of ``ImportExportMixin.import_from_dict`` adapted to take
    an explicit ``session`` argument and operate as a free function.
    """
    if sync is None:
        sync = []

    parent_refs = _parent_foreign_key_mappings(cls)
    export_fields: set[str] = (
        set(getattr(cls, "export_fields", []))
        | set(getattr(cls, "extra_import_fields", []))
        | set(parent_refs.keys())
        | {"uuid"}
    )
    new_children = {
        c: dict_rep[c] for c in getattr(cls, "export_children", []) if c in dict_rep
    }
    unique_constraints = _unique_constraints(cls)

    filters: list[Any] = []  # filters used to check whether obj already exists

    # Strip fields that should not get imported
    for k in list(dict_rep):
        if k not in export_fields and k not in parent_refs:
            del dict_rep[k]

    if not parent:
        if getattr(cls, "export_parent", None):
            for prnt in parent_refs:
                if prnt not in dict_rep:
                    raise RuntimeError(f"{cls.__name__}: Missing field {prnt}")
    else:
        # Set foreign keys to parent obj
        for k, v in parent_refs.items():
            dict_rep[k] = getattr(parent, v)

    if not allow_reparenting:
        # Add filter for parent obj
        filters.extend([getattr(cls, k) == dict_rep.get(k) for k in parent_refs])

    # Add filter for unique constraints
    ucs = [
        and_(
            *[
                getattr(cls, k) == dict_rep.get(k)
                for k in cs
                if dict_rep.get(k) is not None
            ]
        )
        for cs in unique_constraints
    ]
    if ucs:
        filters.append(or_(*ucs))

    # Check if object already exists in DB; raise on duplicates
    try:
        obj_query = session.query(cls).filter(and_(*filters))
        obj = obj_query.one_or_none()
    except MultipleResultsFound:
        logger.error(
            "Error importing %s \n %s \n %s",
            cls.__name__,
            str(obj_query),
            yaml.safe_dump(dict_rep),
            exc_info=True,
        )
        raise

    if not obj:
        is_new_obj = True
        obj = cls(**dict_rep)
        logger.debug("Importing new %s %s", obj.__tablename__, str(obj))
        if getattr(cls, "export_parent", None) and parent:
            setattr(obj, cls.export_parent, parent)  # type: ignore[attr-defined]
        session.add(obj)
    else:
        is_new_obj = False
        logger.debug("Updating %s %s", obj.__tablename__, str(obj))
        for k, v in dict_rep.items():
            setattr(obj, k, v)

    # Recursively create children
    if recursive:
        for child in getattr(cls, "export_children", []):
            argument = cls.__mapper__.relationships[child].argument  # type: ignore[attr-defined]
            child_class = argument.class_ if hasattr(argument, "class_") else argument
            added: list[Any] = []
            for c_obj in new_children.get(child, []) or []:
                added.append(
                    import_from_dict(
                        session,
                        child_class,
                        dict_rep=c_obj,
                        parent=obj,
                        sync=sync,
                    )
                )
            # If children should get synced, delete the ones that did not
            # get updated.
            if child in sync and not is_new_obj:
                back_refs = _parent_foreign_key_mappings(child_class)
                delete_filters = [
                    getattr(child_class, k) == getattr(obj, back_refs.get(k) or "")
                    for k in back_refs
                ]
                to_delete = set(
                    session.query(child_class).filter(and_(*delete_filters))
                ).difference(set(added))
                for o in to_delete:
                    logger.debug("Deleting %s %s", child, str(obj))
                    session.delete(o)

    return obj


# ---------------------------------------------------------------------------
# Database-level helpers
# ---------------------------------------------------------------------------


def lookup_sqla_table(session: Any, table: Any) -> Any | None:
    from superset.models.connectors import SqlaTable
    from superset.models.core import Database

    return (
        session.query(SqlaTable)
        .join(Database)
        .filter(
            SqlaTable.table_name == table.table_name,
            SqlaTable.schema == table.schema,
            Database.id == table.database_id,
        )
        .first()
    )


def lookup_sqla_database(session: Any, table: Any) -> Any | None:
    from superset.commands.database.exceptions import DatabaseNotFoundError
    from superset.models.core import Database

    database = (
        session.query(Database)
        .filter_by(database_name=table.params_dict["database_name"])
        .one_or_none()
    )
    if database is None:
        raise DatabaseNotFoundError()
    return database


def import_dataset(
    session: Any,
    i_datasource: Any,
    database_id: int | None = None,
    import_time: int | None = None,
) -> int:
    """Import a single datasource (SqlaTable) into the database.

    Metrics, columns and the parent dataset are overridden if they exist.
    """
    from superset.models.connectors import SqlaTable

    lookup_database: Callable[[Any], Any | None]
    lookup_datasource: Callable[[Any], Any | None]
    if isinstance(i_datasource, SqlaTable):
        lookup_database = lambda t: lookup_sqla_database(session, t)  # noqa: E731
        lookup_datasource = lambda t: lookup_sqla_table(session, t)  # noqa: E731
    else:
        raise CommandInvalidError(f"Unsupported datasource type: {type(i_datasource)}")

    return _import_datasource(
        session,
        i_datasource,
        lookup_database,
        lookup_datasource,
        import_time,
        database_id,
    )


def _alter_params(obj: Any, **kwargs: Any) -> None:
    """Mirror :func:`ImportExportMixin.alter_params` 1:1."""
    params = obj.params_dict if hasattr(obj, "params_dict") else {}
    params.update(kwargs)
    obj.params = json.dumps(params)


def _override_export_fields(target: Any, source: Any) -> None:
    """Copy every ``export_field`` from ``source`` onto ``target``.

    Mirrors :func:`ImportExportMixin.override` 1:1.
    """
    for field in source.__class__.export_fields:
        setattr(target, field, getattr(source, field))


def _copy_export_fields(obj: Any) -> Any:
    """Build a new instance with every ``export_field`` copied from ``obj``.

    Mirrors :func:`ImportExportMixin.copy` 1:1.
    """
    new_obj = obj.__class__()
    _override_export_fields(new_obj, obj)
    return new_obj


def _import_simple_obj(
    session: Any, i_obj: Any, lookup_obj: Callable[[Any], Any | None]
) -> Any:
    make_transient(i_obj)
    i_obj.id = None
    i_obj.table = None

    existing_column = lookup_obj(i_obj)
    i_obj.table = None
    if existing_column:
        _override_export_fields(existing_column, i_obj)
        session.flush()
        return existing_column

    session.add(i_obj)
    session.flush()
    return i_obj


def lookup_sqla_metric(session: Any, metric: Any) -> Any:
    from superset.models.connectors import SqlMetric

    return (
        session.query(SqlMetric)
        .filter(
            SqlMetric.table_id == metric.table_id,
            SqlMetric.metric_name == metric.metric_name,
        )
        .first()
    )


def lookup_sqla_column(session: Any, column: Any) -> Any:
    from superset.models.connectors import TableColumn

    return (
        session.query(TableColumn)
        .filter(
            TableColumn.table_id == column.table_id,
            TableColumn.column_name == column.column_name,
        )
        .first()
    )


def import_metric(session: Any, metric: Any) -> Any:
    return _import_simple_obj(session, metric, lambda m: lookup_sqla_metric(session, m))


def import_column(session: Any, column: Any) -> Any:
    return _import_simple_obj(session, column, lambda c: lookup_sqla_column(session, c))


def _import_datasource(  # noqa: C901
    session: Any,
    i_datasource: Any,
    lookup_database: Callable[[Any], Any | None],
    lookup_datasource: Callable[[Any], Any | None],
    import_time: int | None = None,
    database_id: int | None = None,
) -> int:
    """Import a datasource — direct port of ``import_datasource``."""
    make_transient(i_datasource)
    logger.info("Started import of the datasource: %s", i_datasource)

    i_datasource.id = None
    i_datasource.database_id = (
        database_id
        if database_id
        else getattr(lookup_database(i_datasource), "id", None)
    )
    _alter_params(i_datasource, import_time=import_time)

    # Override the datasource if it exists.
    datasource = lookup_datasource(i_datasource)

    if datasource:
        _override_export_fields(datasource, i_datasource)
        session.flush()
    else:
        datasource = _copy_export_fields(i_datasource)
        session.add(datasource)
        session.flush()

    for metric in i_datasource.metrics:
        new_m = _copy_export_fields(metric)
        new_m.table_id = datasource.id
        logger.info(
            "Importing metric %s from the datasource: %s",
            getattr(new_m, "metric_name", ""),
            getattr(i_datasource, "table_name", ""),
        )
        imported_m = import_metric(session, new_m)
        if imported_m.metric_name not in [m.metric_name for m in datasource.metrics]:
            datasource.metrics.append(imported_m)

    for column in i_datasource.columns:
        new_c = _copy_export_fields(column)
        new_c.table_id = datasource.id
        logger.info(
            "Importing column %s from the datasource: %s",
            getattr(new_c, "column_name", ""),
            getattr(i_datasource, "table_name", ""),
        )
        imported_c = import_column(session, new_c)
        if imported_c.column_name not in [c.column_name for c in datasource.columns]:
            datasource.columns.append(imported_c)
    session.flush()
    return datasource.id


def import_from_yaml(
    session: Any,
    data: dict[str, Any] | list[dict[str, Any]],
    sync: list[str] | None = None,
) -> None:
    """Import databases from a parsed YAML payload (CLI export shape)."""
    from superset.models.core import Database

    if sync is None:
        sync = []
    if isinstance(data, dict):
        logger.info("Importing %d %s", len(data.get(DATABASES_KEY, [])), DATABASES_KEY)
        for database in data.get(DATABASES_KEY, []):
            import_from_dict(session, Database, database, sync=sync)
    else:
        logger.info("Supplied object is not a dictionary.")


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------


class ImportDatasetsCommand:
    """Import datasources in YAML format (the original unversioned export).

    Direct port of
    ``superset_old/commands/dataset/importers/v0.py:ImportDatasetsCommand``.
    """

    # pylint: disable=unused-argument
    def __init__(
        self,
        contents: dict[str, str],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.contents = contents
        self._configs: dict[str, Any] = {}

        self.sync: list[str] = []
        if kwargs.get("sync_columns"):
            self.sync.append("columns")
        if kwargs.get("sync_metrics"):
            self.sync.append("metrics")

    def run(self, session: Any | None = None) -> None:
        """Validate and run the import.

        ``session`` defaults to the result of
        :func:`superset.db.session.get_sync_session` — supply a session to
        run the import inside an existing transaction (CLI / tests).
        """
        self.validate()

        if session is None:
            from superset.db.session import get_sync_session

            session = get_sync_session()
            owns_session = True
        else:
            owns_session = False

        try:
            from superset.models.connectors import SqlaTable
            from superset.models.core import Database

            for file_name, config in self._configs.items():
                logger.info("Importing dataset from file %s", file_name)
                if isinstance(config, dict):
                    import_from_yaml(session, config, sync=self.sync)
                else:  # list — UI export
                    for dataset in config:
                        # UI exports don't include database metadata, so we
                        # assume the DB already exists and has the same
                        # name (matching the original behaviour).
                        params = json.loads(dataset["params"])
                        database = (
                            session.query(Database)
                            .filter_by(database_name=params["database_name"])
                            .one()
                        )
                        dataset["database_id"] = database.id
                        import_from_dict(session, SqlaTable, dataset, sync=self.sync)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()

    def validate(self) -> None:
        """Validate that every file is YAML in the expected v0 shape."""
        for file_name, content in self.contents.items():
            try:
                config = yaml.safe_load(content)
            except yaml.parser.ParserError as ex:
                logger.exception("Invalid YAML file")
                raise IncorrectVersionError(
                    f"{file_name} is not a valid YAML file"
                ) from ex

            # CLI export — must contain ``databases``.
            if isinstance(config, dict):
                if DATABASES_KEY not in config:
                    raise IncorrectVersionError(f"{file_name} has no valid keys")

            # UI export — list of datasets.  Per-row schema validation is
            # left to ``import_from_dict`` (matching the original).
            elif isinstance(config, list):
                pass

            else:
                raise IncorrectVersionError(f"{file_name} is not a valid file")

            self._configs[file_name] = config


__all__ = [
    "ImportDatasetsCommand",
    "import_dataset",
    "import_from_dict",
    "import_from_yaml",
]
