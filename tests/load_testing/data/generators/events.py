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

"""Events data generator for analytics workloads."""

import json
import uuid
from typing import Any

from .base import DataGenerator


class EventsGenerator(DataGenerator):
    """
    Generates event tracking data.
    Target: 100M+ rows for ClickHouse.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.event_types = [
            "page_view",
            "click",
            "scroll",
            "form_submit",
            "purchase",
            "signup",
            "login",
            "logout",
            "search",
            "add_to_cart",
            "remove_from_cart",
            "checkout_start",
            "checkout_complete",
            "video_play",
            "video_pause",
            "download",
            "share",
            "like",
            "comment",
            "subscribe",
            "unsubscribe",
            "error",
            "api_call",
        ]

        # Weights for event types (page_view most common)
        self.event_weights = [
            30,
            20,
            10,
            5,
            3,
            2,
            5,
            2,
            8,
            4,
            2,
            2,
            1,
            3,
            2,
            2,
            2,
            3,
            2,
            1,
            1,
            1,
            2,
        ]

        self.sources = ["organic", "paid", "social", "email", "direct", "referral"]
        self.campaigns = [f"campaign_{i}" for i in range(1, 51)]
        self.pages = [f"/page/{i}" for i in range(1, 101)]
        self.user_pool_size = 1000000  # 1M unique users

    def generate_row(self) -> dict[str, Any]:
        """Generate a single event row."""
        event_type = self.weighted_choice(self.event_types, self.event_weights)
        timestamp = self.random_timestamp()
        user_id = self.random_int(1, self.user_pool_size)

        # Generate event-specific properties
        properties = self._generate_properties(event_type)

        return {
            "event_id": str(uuid.uuid4()),
            "user_id": user_id,
            "event_type": event_type,
            "timestamp": timestamp.isoformat(),
            "properties": json.dumps(properties),
            "session_id": str(uuid.uuid4()),
            "platform": self.random_choice(self.platforms),
            "country": self.random_choice(self.countries),
            "region": f"region_{self.random_int(1, 50)}",
            "device_type": self.random_choice(self.devices),
            "browser": self.random_choice(self.browsers),
            "value": self.random_float(0, 1000),
            "duration_ms": self.random_int(0, 60000),
            "is_converted": 1 if self.random_bool(0.1) else 0,
        }

    def _generate_properties(self, event_type: str) -> dict[str, Any]:
        """Generate event-specific properties."""
        base_props = {
            "source": self.random_choice(self.sources),
            "campaign": self.random_choice(self.campaigns)
            if self.random_bool(0.3)
            else None,
            "page": self.random_choice(self.pages),
        }

        if event_type == "purchase":
            base_props.update(
                {
                    "amount": self.random_float(10, 500),
                    "currency": "USD",
                    "items_count": self.random_int(1, 10),
                    "payment_method": self.random_choice(
                        ["card", "paypal", "apple_pay"]
                    ),
                }
            )
        elif event_type == "search":
            base_props.update(
                {
                    "query": f"search_term_{self.random_int(1, 1000)}",
                    "results_count": self.random_int(0, 100),
                }
            )
        elif event_type == "video_play":
            base_props.update(
                {
                    "video_id": f"video_{self.random_int(1, 500)}",
                    "duration_seconds": self.random_int(30, 3600),
                    "watched_percent": self.random_float(0, 100),
                }
            )
        elif event_type in ["click", "form_submit"]:
            base_props.update(
                {
                    "element_id": f"element_{self.random_int(1, 100)}",
                    "element_type": self.random_choice(
                        ["button", "link", "input", "form"]
                    ),
                }
            )

        return {k: v for k, v in base_props.items() if v is not None}

    def get_schema(self) -> dict[str, str]:
        """Get ClickHouse schema for events table."""
        return {
            "event_id": "UUID",
            "user_id": "UInt64",
            "event_type": "LowCardinality(String)",
            "timestamp": "DateTime64(3)",
            "properties": "String",
            "session_id": "String",
            "platform": "LowCardinality(String)",
            "country": "LowCardinality(String)",
            "region": "LowCardinality(String)",
            "device_type": "LowCardinality(String)",
            "browser": "LowCardinality(String)",
            "value": "Float64",
            "duration_ms": "UInt32",
            "is_converted": "UInt8",
        }
