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

from sqlalchemy import BigInteger, Float, Text

from superset.examples import _ctx
from superset.examples.helpers import get_table_connector_registry, read_example_data
from superset.utils import json

logger = logging.getLogger(__name__)


def load_sf_population_polygons(
    only_metadata: bool = False, force: bool = False
) -> None:
    tbl_name = "sf_population_polygons"
    database = _ctx.get_example_database()
    with _ctx.example_engine(database) as engine:
        schema = _ctx.get_schema(engine)
        table_exists = _ctx.has_table(engine, tbl_name, schema)

        if not only_metadata and (not table_exists or force):
            df = read_example_data(
                "examples://sf_population.json.gz", compression="gzip"
            )
            df["contour"] = df.contour.map(json.dumps)

            df.to_sql(
                tbl_name,
                engine,
                schema=schema,
                if_exists="replace",
                chunksize=500,
                dtype={
                    "zipcode": BigInteger,
                    "population": BigInteger,
                    "contour": Text,
                    "area": Float,
                },
                index=False,
            )

    logger.debug(f"Creating table {tbl_name} reference")
    table = get_table_connector_registry()
    tbl = _ctx.session.query(table).filter_by(table_name=tbl_name).first()
    if not tbl:
        tbl = table(table_name=tbl_name, schema=schema)
        _ctx.session.add(tbl)
    tbl.description = "Population density of San Francisco"
    tbl.database = database
    tbl.filter_select_enabled = True
