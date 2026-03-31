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

Signature-compatible stub that matches the original Superset
``load-examples`` command.  Full data loading is not yet implemented
for the async backend.
"""

from __future__ import annotations

import click


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
    """Load a set of Slices, Dashboards, and a supporting dataset.

    NOTE: Full example data loading is not yet implemented in the async
    backend.  This command currently serves as a stub with the correct
    interface for backward compatibility.
    """
    click.secho(
        "WARNING: load-examples is not yet fully implemented in Liteset.\n"
        "This is a stub preserving the original CLI interface.\n"
        "Run the original Superset's load_examples if you need the data.",
        fg="yellow",
    )
    click.echo(f"  Options: test_data={load_test_data}, big_data={load_big_data}, "
               f"metadata_only={only_metadata}, force={force}")
    click.echo("TODO: Implement async example data loading.")
