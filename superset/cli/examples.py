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
"""Load example data command.

Loads example datasets, charts, and dashboards into the Superset
metadata database.  Uses a synchronous SQLAlchemy session since the
CLI runs outside the async event loop and pandas ``to_sql`` requires
a sync engine.
"""
from __future__ import annotations

import logging

import click

logger = logging.getLogger(__name__)


def load_examples_run(
    load_test_data: bool = False,
    load_big_data: bool = False,
    only_metadata: bool = False,
    force: bool = False,
) -> None:
    from superset.examples import _ctx
    from superset.examples import data_loading as examples

    if only_metadata:
        logger.info("Loading examples metadata")
    else:
        examples_db = _ctx.get_example_database()
        logger.info("Loading examples metadata and data into %s", examples_db)

    examples.load_css_templates()
    _ctx.commit()

    if load_test_data:
        logger.info("Loading energy related dataset")
        examples.load_energy(only_metadata, force)
        _ctx.commit()

    logger.info("Loading [World Bank's Health Nutrition and Population Stats]")
    examples.load_world_bank_health_n_pop(only_metadata, force)
    _ctx.commit()

    logger.info("Loading [Birth names]")
    examples.load_birth_names(only_metadata, force)
    _ctx.commit()

    if load_test_data:
        logger.info("Loading [Tabbed dashboard]")
        examples.load_tabbed_dashboard(only_metadata)
        _ctx.commit()

        logger.info("Loading [Supported Charts Dashboard]")
        examples.load_supported_charts_dashboard()
        _ctx.commit()
    else:
        logger.info("Loading [Random long/lat data]")
        examples.load_long_lat_data(only_metadata, force)
        _ctx.commit()

        logger.info("Loading [Country Map data]")
        examples.load_country_map_data(only_metadata, force)
        _ctx.commit()

        logger.info("Loading [San Francisco population polygons]")
        examples.load_sf_population_polygons(only_metadata, force)
        _ctx.commit()

        logger.info("Loading [Flights data]")
        examples.load_flights(only_metadata, force)
        _ctx.commit()

        logger.info("Loading [BART lines]")
        examples.load_bart_lines(only_metadata, force)
        _ctx.commit()

        logger.info("Loading [Misc Charts] dashboard")
        examples.load_misc_dashboard()
        _ctx.commit()

        logger.info("Loading DECK.gl demo")
        examples.load_deck_dash()
        _ctx.commit()

    if load_big_data:
        logger.info("Loading big synthetic data for tests")
        examples.load_big_data()
        _ctx.commit()


@click.command("load-examples")
@click.option(
    "--load-test-data",
    "-t",
    is_flag=True,
    help="Load additional test data",
)
@click.option(
    "--load-big-data",
    "-b",
    is_flag=True,
    help="Load additional big data",
)
@click.option(
    "--only-metadata",
    "-m",
    is_flag=True,
    help="Only load metadata, skip actual data",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Force load data even if table already exists",
)
def load_examples(
    load_test_data: bool = False,
    load_big_data: bool = False,
    only_metadata: bool = False,
    force: bool = False,
) -> None:
    """Load a set of Slices, Dashboards, and a supporting dataset."""
    from superset.examples import _ctx

    click.echo("Initialising database context...")
    _ctx.init()
    try:
        load_examples_run(load_test_data, load_big_data, only_metadata, force)
        _ctx.commit()
        click.secho("Example data loaded successfully.", fg="green")
    except Exception:
        logger.exception("Failed to load examples")
        if _ctx.session is not None:
            _ctx.session.rollback()
        raise
    finally:
        _ctx.teardown()
