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

import pandas as pd
from sqlalchemy import DateTime, String

from superset.examples import _ctx
from superset.examples._ctx import DatasourceType
from superset.examples.helpers import (
    get_slice_json,
    get_table_connector_registry,
    merge_slice,
    read_example_data,
)
from superset.models.slice import Slice

logger = logging.getLogger(__name__)


def load_random_time_series_data(
    only_metadata: bool = False, force: bool = False
) -> None:
    """Loading random time series data from a zip file in the repo"""
    tbl_name = "random_time_series"
    database = _ctx.get_example_database()
    backend = _ctx.get_backend(database)
    with _ctx.example_engine(database) as engine:
        schema = _ctx.get_schema(engine)
        table_exists = _ctx.has_table(engine, tbl_name, schema)

        if not only_metadata and (not table_exists or force):
            pdf = read_example_data(
                "examples://random_time_series.json.gz", compression="gzip"
            )
            if backend == "presto":
                pdf.ds = pd.to_datetime(pdf.ds, unit="s")
                pdf.ds = pdf.ds.dt.strftime("%Y-%m-%d %H:%M%:%S")
            else:
                pdf.ds = pd.to_datetime(pdf.ds, unit="s")

            pdf.to_sql(
                tbl_name,
                engine,
                schema=schema,
                if_exists="replace",
                chunksize=500,
                dtype={"ds": DateTime if backend != "presto" else String(255)},
                index=False,
            )
        logger.debug("Done loading table!")
        logger.debug("-" * 80)

    logger.debug("Creating table [%s] reference", tbl_name)
    table = get_table_connector_registry()
    obj = _ctx.session.query(table).filter_by(table_name=tbl_name).first()
    if not obj:
        obj = table(table_name=tbl_name, schema=schema)
        _ctx.session.add(obj)
    obj.main_dttm_col = "ds"
    obj.database = database
    obj.filter_select_enabled = True
    tbl = obj

    slice_data = {
        "granularity_sqla": "ds",
        "row_limit": _ctx.row_limit,
        "since": "2019-01-01",
        "until": "2019-02-01",
        "metrics": ["count"],
        "viz_type": "cal_heatmap",
        "domain_granularity": "month",
        "subdomain_granularity": "day",
    }

    logger.debug("Creating a slice")
    slc = Slice(
        slice_name="Calendar Heatmap",
        viz_type="cal_heatmap",
        datasource_type=DatasourceType.TABLE,
        datasource_id=tbl.id,
        params=get_slice_json(slice_data),
    )
    merge_slice(slc)
