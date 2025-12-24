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

"""Base data generator class."""

import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Generator

logger = logging.getLogger(__name__)


class DataGenerator(ABC):
    """
    Abstract base class for data generators.
    Generates large volumes of test data for load testing.
    """

    def __init__(
        self,
        batch_size: int = 100000,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        seed: int | None = None,
    ):
        self.batch_size = batch_size
        self.start_date = start_date or datetime(2020, 1, 1)
        self.end_date = end_date or datetime.now()

        if seed is not None:
            random.seed(seed)

        # Common data pools
        self.countries = [
            "US",
            "UK",
            "DE",
            "FR",
            "JP",
            "BR",
            "IN",
            "CA",
            "AU",
            "MX",
            "IT",
            "ES",
            "NL",
            "KR",
            "SG",
            "SE",
            "CH",
            "BE",
            "AT",
            "PL",
        ]
        self.platforms = ["web", "ios", "android", "desktop"]
        self.browsers = ["Chrome", "Firefox", "Safari", "Edge", "Opera"]
        self.devices = ["desktop", "mobile", "tablet"]

    @abstractmethod
    def generate_row(self) -> dict[str, Any]:
        """Generate a single row of data."""
        pass

    @abstractmethod
    def get_schema(self) -> dict[str, str]:
        """Get schema definition for the data."""
        pass

    def generate_batch(self) -> list[dict[str, Any]]:
        """Generate a batch of rows."""
        return [self.generate_row() for _ in range(self.batch_size)]

    def generate_batches(self, total_rows: int) -> Generator[list[dict], None, None]:
        """Generate multiple batches to reach total_rows."""
        generated = 0
        while generated < total_rows:
            batch_size = min(self.batch_size, total_rows - generated)
            yield [self.generate_row() for _ in range(batch_size)]
            generated += batch_size
            if generated % 1000000 == 0:
                logger.info(f"Generated {generated:,} rows")

    def random_timestamp(self) -> datetime:
        """Generate random timestamp within date range."""
        delta = self.end_date - self.start_date
        random_seconds = random.randint(0, int(delta.total_seconds()))
        return self.start_date + timedelta(seconds=random_seconds)

    def random_choice(self, items: list) -> Any:
        """Random choice from list."""
        return random.choice(items)

    def random_int(self, min_val: int, max_val: int) -> int:
        """Random integer in range."""
        return random.randint(min_val, max_val)

    def random_float(self, min_val: float, max_val: float, decimals: int = 2) -> float:
        """Random float in range."""
        return round(random.uniform(min_val, max_val), decimals)

    def random_bool(self, true_probability: float = 0.5) -> bool:
        """Random boolean with specified probability."""
        return random.random() < true_probability

    def weighted_choice(self, items: list, weights: list[float]) -> Any:
        """Weighted random choice."""
        return random.choices(items, weights=weights, k=1)[0]
