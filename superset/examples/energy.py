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
import logging
import textwrap

from sqlalchemy import Float, String
from sqlalchemy.sql import column

from superset.examples import _ctx
from superset.examples._ctx import DatasourceType
from superset.examples.helpers import (
    get_slice_json,
    get_table_connector_registry,
    merge_slice,
    misc_dash_slices,
    read_example_data,
)
from superset.models.connectors import SqlMetric
from superset.models.slice import Slice

logger = logging.getLogger(__name__)


def load_energy(
    only_metadata: bool = False, force: bool = False, sample: bool = False
) -> None:
    """Loads an energy related dataset to use with sankey and graphs"""
    tbl_name = "energy_usage"
    database = _ctx.get_example_database()

    with _ctx.example_engine(database) as engine:
        schema = _ctx.get_schema(engine)
        table_exists = _ctx.has_table(engine, tbl_name, schema)

        if not only_metadata and (not table_exists or force):
            pdf = read_example_data("examples://energy.json.gz", compression="gzip")
            pdf = pdf.head(100) if sample else pdf
            pdf.to_sql(
                tbl_name,
                engine,
                schema=schema,
                if_exists="replace",
                chunksize=500,
                dtype={"source": String(255), "target": String(255), "value": Float()},
                index=False,
                method="multi",
            )

    logger.debug("Creating table [wb_health_population] reference")
    table = get_table_connector_registry()
    tbl = _ctx.session.query(table).filter_by(table_name=tbl_name).first()
    if not tbl:
        tbl = table(table_name=tbl_name, schema=schema)
        _ctx.session.add(tbl)
    tbl.description = "Energy consumption"
    tbl.database = database
    tbl.filter_select_enabled = True

    with _ctx.example_engine(database) as eng:
        _ctx.fetch_table_metadata(tbl, eng)

    if not any(col.metric_name == "sum__value" for col in tbl.metrics):
        col = str(column("value").compile(_ctx.engine))
        tbl.metrics.append(
            SqlMetric(metric_name="sum__value", expression=f"SUM({col})")
        )

    slc = Slice(
        slice_name="Energy Sankey",
        viz_type="sankey_v2",
        datasource_type=DatasourceType.TABLE,
        datasource_id=tbl.id,
        params=textwrap.dedent(
            """\
        {
            "collapsed_fieldsets": "",
            "source": "source",
            "target": "target",
            "metric": "sum__value",
            "row_limit": "5000",
            "slice_name": "Energy Sankey",
            "viz_type": "sankey_v2"
        }
        """
        ),
    )
    misc_dash_slices.add(str(slc.slice_name))
    merge_slice(slc)

    slc = Slice(
        slice_name="Energy Force Layout",
        viz_type="graph_chart",
        datasource_type=DatasourceType.TABLE,
        datasource_id=tbl.id,
        params=textwrap.dedent(
            """\
        {
            "source": "source",
            "target": "target",
            "edgeLength": 400,
            "repulsion": 1000,
            "layout": "force",
            "metric": "sum__value",
            "row_limit": "5000",
            "slice_name": "Force",
            "viz_type": "graph_chart"
        }
        """
        ),
    )
    misc_dash_slices.add(str(slc.slice_name))
    merge_slice(slc)

    slc = Slice(
        slice_name="Heatmap",
        viz_type="heatmap_v2",
        datasource_type=DatasourceType.TABLE,
        datasource_id=tbl.id,
        params=get_slice_json(
            defaults={},
            viz_type="heatmap_v2",
            x_axis="source",
            groupby="target",
            legend_type="continuous",
            metric="sum__value",
            sort_x_axis="value_asc",
            sort_y_axis="value_asc",
        ),
    )
    misc_dash_slices.add(str(slc.slice_name))
    merge_slice(slc)
