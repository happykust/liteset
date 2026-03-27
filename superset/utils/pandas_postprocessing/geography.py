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
from typing import Optional

import geohash as geohash_lib
from geopy.point import Point
from pandas import DataFrame

from superset.exceptions import InvalidPostProcessingError
from superset.utils.pandas_postprocessing.utils import _append_columns


def geohash_decode(
    df: DataFrame, geohash: str, longitude: str, latitude: str
) -> DataFrame:
    """Decode a geohash column into longitude and latitude."""
    try:
        lonlat_df = DataFrame()
        lonlat_df["latitude"], lonlat_df["longitude"] = zip(
            *df[geohash].apply(geohash_lib.decode), strict=False
        )
        return _append_columns(
            df, lonlat_df, {"latitude": latitude, "longitude": longitude}
        )
    except ValueError as ex:
        raise InvalidPostProcessingError("Invalid geohash string") from ex


def geohash_encode(
    df: DataFrame,
    geohash: str,
    longitude: str,
    latitude: str,
) -> DataFrame:
    """Encode longitude and latitude into geohash."""
    try:
        encode_df = df[[latitude, longitude]]
        encode_df.columns = ["latitude", "longitude"]
        encode_df["geohash"] = encode_df.apply(
            lambda row: geohash_lib.encode(row["latitude"], row["longitude"]),
            axis=1,
        )
        return _append_columns(df, encode_df, {"geohash": geohash})
    except ValueError as ex:
        raise InvalidPostProcessingError("Invalid longitude/latitude") from ex


def geodetic_parse(
    df: DataFrame,
    geodetic: str,
    longitude: str,
    latitude: str,
    altitude: Optional[str] = None,
) -> DataFrame:
    """Parse a column containing a geodetic point string."""

    def _parse_location(location: str) -> tuple[float, float, float]:
        point = Point(location)
        return point[0], point[1], point[2]

    try:
        geodetic_df = DataFrame()
        (
            geodetic_df["latitude"],
            geodetic_df["longitude"],
            geodetic_df["altitude"],
        ) = zip(*df[geodetic].apply(_parse_location), strict=False)
        columns = {"latitude": latitude, "longitude": longitude}
        if altitude:
            columns["altitude"] = altitude
        return _append_columns(df, geodetic_df, columns)
    except ValueError as ex:
        raise InvalidPostProcessingError("Invalid geodetic string") from ex
